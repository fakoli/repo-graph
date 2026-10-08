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
    def test_ranking_cli_refuses_inputs_without_importing_producer(self):
        root = Path(__file__).resolve().parents[1]
        from evaluations.acceptance import _ranking_inputs
        _, _, _, inputs = _ranking_inputs(root)
        command = '''
import importlib.abc, os, runpy, sys
from pathlib import Path
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('evaluations.analysis', 'evaluations.tree_sitter_baseline', 'repo_graph.analysis_native'):
            os._exit(97)
sys.meta_path.insert(0, Guard())
script = Path(sys.argv[1])
sys.path.insert(0, str(script.parents[1]))
sys.argv = [str(script), '--gate', 'task-preflight', '--task', 'T047', '--checks', 'ranking-boundary']
runpy.run_path(str(script), run_name='__main__')
'''
        with tempfile.TemporaryDirectory() as scratch:
            parent = Path(scratch)
            def copy(destination):
                for path in (*inputs, 'evaluations/acceptance.py', 'repo_graph/__init__.py', 'repo_graph/source.py'):
                    target = destination / path; target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((root / path).read_bytes())
                report = destination / 'evaluations/results/code-understanding/business.json'
                report.parent.mkdir(parents=True, exist_ok=True); report.write_bytes(b'preserved evidence')
                return report
            for control in ('uncommitted', 'changed', 'malformed', 'ancestor_git'):
                with self.subTest(control=control):
                    draft = parent / control; report = copy(draft)
                    if control == 'changed':
                        source = draft / next(path for path in inputs if '/retrieval/' in path)
                        source.write_bytes(source.read_bytes() + b'\n')
                    elif control == 'malformed':
                        (draft / 'evaluations/code-understanding/function-relevance-review.json').write_bytes(b'{')
                    elif control == 'ancestor_git':
                        copy(parent)
                        for argv in (['init', '-q'], ['add', '--', *inputs],
                                ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Synthetic source key']):
                            subprocess.run(['git', '-C', str(parent), *argv], check=True,
                                           capture_output=True, timeout=5)
                    result = subprocess.run([sys.executable, '-I', '-c', command,
                        str(draft / 'evaluations/acceptance.py')], capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    observation = json.loads(result.stdout)
                    self.assertEqual((observation['status'], observation['stopped_phase']), ('blocked', 'inputs'))
                    self.assertEqual(report.read_bytes(), b'preserved evidence')

    def test_impact_cli_selectors_filters_and_producer_git_base_forwarding(self):
        from repo_graph import cli, analysis, analysis_queries
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir(); out.mkdir()
            (out / 'search.db').touch()
            calls = []
            class Session:
                owner = 'synthetic-owner'
                def __init__(self, *args): pass
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def run(self, payload): calls.append(payload); return dict(cursor=None, stub=True)
            base = 'a' * 40
            with patch.object(analysis_queries, 'Queries', Session), redirect_stdout(io.StringIO()), \
                    patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(cli.main(['query', str(out), '--operation', 'impact', '--source-area', 'pkg/',
                    '--source-area', 'main.py', '--relation', 'import', '--certainty', 'candidate']), 0)
                self.assertEqual(calls[-1]['selector'], dict(kind='source_area', paths=['pkg/', 'main.py']))
                self.assertEqual(calls[-1]['relations'], ['import']); self.assertEqual(calls[-1]['certainties'], ['candidate'])
                self.assertEqual(cli.main(['query', str(out), '--operation', 'impact', '--git-base', base]), 0)
                self.assertEqual(calls[-1]['selector'], dict(kind='git_change', base_revision=base))
                self.assertEqual(cli.main(['query', str(out), '--operation', 'call', '--relation', 'import']), 1)
                self.assertEqual(cli.main(['query', str(out), '--operation', 'impact', '--stdio', '--source-area', '.']), 1)
                for argv in (['query', str(out), '--git-base', 'main'],
                        ['analyze', str(root), '--output', str(out), '--git-base', 'HEAD'],
                        ['query', str(out), '--source-area', '.', '--git-base', base]):
                    with self.assertRaises(SystemExit): cli.main(argv)
                self.assertEqual(len(calls), 2)
            with patch.object(cli.builder, 'repo_files', return_value=['main.py']), \
                    patch.object(analysis, 'StructuralIndex') as index, redirect_stdout(io.StringIO()):
                index.return_value.refresh.return_value = dict(status='ready', stub=True)
                self.assertEqual(cli.main(['analyze', str(root), '--output', str(out), '--git-base', base]), 0)
                self.assertEqual(index.return_value.refresh.call_args.kwargs['git_base'], base)

    def test_captured_import_source_multilanguage_and_http_share_snapshot_contract(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        import hashlib
        from repo_graph import analysis_native
        from repo_graph.analysis import StructuralIndex
        from repo_graph.analysis_queries import Queries, encoded
        secret = 'sk-' + 'A' * 24
        sources = {
            'helper.py': 'def leaf(): pass\n', 'main.py': 'from .helper import leaf\ndef start(): pass\n',
            'helper.js': 'export function leaf() {}\n',
            'main.js': 'import {leaf as café} from "./helper.js";\nimport unused from "' + secret + '";\n',
            'helper.ts': 'export interface Item { value: number }\n', 'main.ts': 'import type {Item} from "./helper";\n',
            'go.mod': 'module example.test/captured\n\ngo 1.22\n',
            'main.go': 'package app\nimport "example.test/captured/lib"\nfunc Start() {}\n',
            'lib/a.go': 'package lib\nfunc A() {}\n', 'lib/b.go': 'package lib\nfunc B() {}\n'}
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            for name, body in sources.items():
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(body)
            receipt = StructuralIndex(root, out).refresh(sources)
            self.assertEqual(receipt['status'], 'ready', receipt)
            request = dict(operation='impact', selector=dict(kind='source_area', paths=['.']), relations=['import'])
            with Queries(out) as queries: impact = queries.run(request)
            rows = [row for row in impact['rows'] if row['relation'] == 'import']
            self.assertTrue(rows)
            engine = search.Search(out); languages, redacted = set(), False
            for name in sources: (root / name).unlink()
            forbidden = AssertionError('Import inspection must remain captured; no source, parser, Git or model')
            with patch.object(SourceRoot, 'read', side_effect=forbidden), \
                    patch.object(StructuralIndex, 'refresh', side_effect=forbidden), \
                    patch.object(analysis_native, 'backend', side_effect=forbidden), \
                    patch.object(search.Embeddings, '__init__', side_effect=forbidden), \
                    patch.object(subprocess, 'run', side_effect=forbidden):
                for row in rows:
                    handle = {key: row['site'][key] for key in ('id', 'path', 'range', 'source_sha256')}
                    observed = search.captured_source(engine, dict(generation=impact['generation'], handle=handle, max_excerpt_bytes=48))
                    languages.add(observed['language']); redacted |= observed['redacted']
                    self.assertEqual(observed['kind'], 'import'); self.assertEqual(observed['role'], 'import')
                    self.assertEqual(observed['certainty'], row['certainty']); self.assertFalse(observed['targets_exhaustive'])
                    self.assertEqual(observed['impact_identity'], impact['impact_identity'])
                    self.assertEqual(observed['handle'], handle); self.assertLessEqual(len(observed['text'].encode()), 48)
                    raw = sources[handle['path']].encode()[observed['range']['start_byte']:observed['range']['end_byte']]
                    self.assertEqual(observed['raw_digest'], hashlib.sha256(raw).hexdigest())
                    self.assertNotIn(secret, observed['text']); self.assertLessEqual(len(search._evidence_encoded(observed)), 32768)
            self.assertEqual(languages, {'python', 'go', 'javascript', 'typescript'}); self.assertTrue(redacted)
            self.assertGreater(len([row for row in rows if row['site']['path'] == 'main.go']), 1)
            with create_server(engine) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
                address = f'http://127.0.0.1:{server.server_port}'
                def post(endpoint, payload, headers=None):
                    return urlopen(Request(address + endpoint, encoded(payload), headers={'Content-Type': 'application/json', **(headers or {})}))
                try:
                    with post('/api/query', dict(operation='impact', selector=dict(kind='source_area', paths=['helper.py']),
                            relations=['import'], certainties=['resolved'])) as response:
                        current = json.loads(response.read())
                    handle = {key: current['rows'][0]['site'][key] for key in ('id', 'path', 'range', 'source_sha256')}
                    payload = dict(generation=current['generation'], handle=handle)
                    with post('/api/source', payload) as response:
                        observed = json.loads(response.read())
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    self.assertEqual(observed['kind'], 'import'); self.assertEqual(observed['impact_identity'], current['impact_identity'])
                    for values, headers, code in ((dict(payload, generation='0' * 64), {}, 409),
                            (dict(payload, handle=dict(handle, source_sha256='0' * 64)), {}, 400),
                            (payload, {'X-Repo-Graph-Output': '0' * 64}, 409)):
                        with self.assertRaises(HTTPError) as caught: post('/api/source', values, headers)
                        self.assertEqual(caught.exception.code, code); caught.exception.close()
                finally: server.shutdown(); thread.join()

    def test_captured_import_source_rejects_forgery_and_old_projection_is_optional(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        from repo_graph.analysis import StructuralIndex
        from repo_graph.analysis_queries import Queries, encoded
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            (root / 'main.py').write_text('from .helper import leaf\ndef start():\n    leaf()\n'); (root / 'helper.py').write_text('def leaf(): pass\n')
            index = StructuralIndex(root, out); receipt = index.refresh(['main.py', 'helper.py'])
            self.assertEqual(receipt['status'], 'ready', receipt)
            with Queries(out) as queries:
                result = queries.run(dict(operation='impact', selector=dict(kind='source_area', paths=['helper.py']), relations=['import']))
            handle = {key: result['rows'][0]['site'][key] for key in ('id', 'path', 'range', 'source_sha256')}
            request = dict(generation=receipt['generation'], handle=handle)
            engine = search.Search(out); self.addCleanup(engine.close)
            with closing(search.connect(out, readonly=True)) as db:
                original = db.execute('SELECT data FROM structural_import_relationships WHERE id=?', (handle['id'],)).fetchone()[0]
            for key, value in (('source_sha256', '0' * 64), ('targets_exhaustive', True),
                    ('range', dict(handle['range'], start_byte=1))):
                changed = json.loads(original); changed[key] = value
                with closing(search.connect(out)) as db, db:
                    db.execute('UPDATE structural_import_relationships SET data=? WHERE id=?', (encoded(changed), handle['id']))
                with self.subTest(key=key), self.assertRaises(ValueError): search.captured_source(engine, request)
            with closing(search.connect(out)) as db, db:
                db.execute('UPDATE structural_import_relationships SET data=? WHERE id=?', (original, handle['id']))
                forged = json.loads(original); forged['id'] = 'import:' + 'a' * 64
                db.execute('UPDATE structural_import_relationships SET id=?,data=? WHERE id=?', (forged['id'], encoded(forged), handle['id']))
            with self.assertRaisesRegex(ValueError, 'ID differs'):
                search.captured_source(engine, dict(request, handle=dict(handle, id=forged['id'])))
            with closing(search.connect(out)) as db, db:
                db.execute('UPDATE structural_import_relationships SET id=?,data=? WHERE id=?', (handle['id'], original, forged['id']))
            with self.assertRaises(InterruptedError): search.captured_source(engine, request, cancel=lambda: True)
            with self.assertRaises(ValueError): search.captured_source(engine, dict(request, max_excerpt_bytes=8193))
            declaration = next(index.read_facts('definitions'))
            physical = {key: declaration[key] for key in ('id', 'path', 'range')} | {'source_sha256': declaration['provenance']['source_sha256']}
            # File navigation handles are never fabricated into source excerpts.
            with self.assertRaises(ValueError):
                search.captured_source(engine, dict(request, handle=dict(handle, id=result['selected_files'][0]['id'])))
            with closing(search.connect(out)) as db, db:
                db.execute("DELETE FROM meta WHERE key IN ('structural_impact_schema','structural_impact_receipt')")
                db.execute('DROP TABLE structural_import_relationships'); db.execute('DROP TABLE structural_git_changes')
            self.assertEqual(search.captured_source(engine, dict(request, handle=physical))['kind'], 'declaration')
            for facts, kind in (('definitions', 'declaration'), ('sites', 'callsite')):
                ordinary = next(row for row in index.read_facts(facts) if row['path'] == 'main.py')
                ordinary_handle = {key: ordinary[key] for key in ('id', 'path', 'range')} | {'source_sha256': ordinary['provenance']['source_sha256']}
                observed = search.captured_source(engine, dict(request, handle=ordinary_handle))
                self.assertEqual(observed['kind'], kind); self.assertEqual(observed['text'], ordinary['text'])
                self.assertEqual(observed['handle'], ordinary_handle)
                # Worker admission excludes colon filenames. This narrow forged ID tests
                # ordinary dispatch/membership only, without pretending native acceptance.
                with self.assertRaisesRegex(ValueError, 'Unknown or ambiguous captured source handle'):
                    search.captured_source(engine, dict(request,
                        handle=dict(ordinary_handle, id='import:' + ordinary_handle['id'])))
            with self.assertRaises(ValueError): search.captured_source(engine, request)
            status = search.index_status(out)
            self.assertTrue(status['structural']['query_available']); self.assertEqual(status['structural']['impact']['state'], 'unavailable')
            with Queries(out) as queries: self.assertTrue(queries.run(dict(operation='symbol'))['rows'])

    def test_captured_impact_git_status_allowlist_and_interface_invalidation(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        import hashlib
        from repo_graph.cli import main
        from repo_graph.analysis import StructuralIndex
        from repo_graph.analysis_queries import encoded
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            def git(*args):
                return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=root,
                    capture_output=True, text=True, check=True, timeout=5).stdout.strip()
            git('init', '-q')
            for path in ('main.py', 'other.py'): (root / path).write_text('from .helper import leaf\ndef start(): pass\n')
            (root / 'helper.py').write_text('def leaf(): pass\n')
            git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (root / 'helper.py').write_text('def leaf():\n    return 1\n')
            git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'change')
            buffer = io.StringIO()
            with redirect_stdout(buffer): self.assertEqual(main(['analyze', str(root), '--output', str(out), '--git-base', base]), 0)
            receipt = json.loads(buffer.getvalue())
            status = search.index_status(out); captured = status['structural']['impact']
            self.assertEqual(captured['state'], 'ready'); self.assertFalse(captured['live_source_observed'])
            self.assertEqual(captured['receipt']['git_change']['base_revision'], base)
            self.assertEqual(captured['receipt']['git_change']['source_byte_affinity'], 'unobserved_worktree')
            buffer = io.StringIO()
            with patch.object(subprocess, 'run', side_effect=AssertionError('No query-time Git/source worker')), redirect_stdout(buffer):
                self.assertEqual(main(['query', str(out), '--operation', 'impact', '--git-base', base,
                    '--relation', 'import', '--certainty', 'resolved']), 0)
            current = json.loads(buffer.getvalue()); self.assertEqual(current['generation'], receipt['generation'])
            self.assertEqual(len(current['rows']), 2)
            with closing(search.connect(out, readonly=True)) as db:
                old_impact = db.execute("SELECT value FROM meta WHERE key='structural_impact_receipt'").fetchone()[0]
                old_foundation = db.execute("SELECT value FROM meta WHERE key='structural_receipt'").fetchone()[0]
            canary = 'PRIVATE_IMPACT_METADATA_CANARY'
            for kind in ('extra', 'nested_extra', 'reason', 'oversized'):
                changed = json.loads(old_impact)
                if kind in ('extra', 'oversized'): changed['operator_note'] = canary * (2000 if kind == 'oversized' else 1)
                elif kind == 'nested_extra': changed['git_change']['operator_note'] = canary
                else: changed['git_change']['reason'] = canary
                changed['identity'] = hashlib.sha256(encoded({key: value for key, value in changed.items() if key != 'identity'})).hexdigest()
                foundation = json.loads(old_foundation); foundation['impact_identity'] = changed['identity']
                with closing(search.connect(out)) as db, db:
                    db.execute("UPDATE meta SET value=? WHERE key='structural_impact_receipt'", (encoded(changed).decode(),))
                    db.execute("UPDATE meta SET value=? WHERE key='structural_receipt'", (encoded(foundation).decode(),))
                safe = search.index_status(out)
                with self.subTest(kind=kind):
                    self.assertEqual(safe['structural']['impact']['state'], 'unavailable')
                    self.assertTrue(safe['structural']['query_available']); self.assertNotIn(canary, json.dumps(safe))
            with closing(search.connect(out)) as db, db:
                db.execute("UPDATE meta SET value=? WHERE key='structural_impact_receipt'", (old_impact,))
                db.execute("UPDATE meta SET value=? WHERE key='structural_receipt'", (old_foundation,))
            with create_server(search.Search(out)) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
                address = f'http://127.0.0.1:{server.server_port}'
                request = dict(operation='impact', selector=dict(kind='source_area', paths=['helper.py']),
                    relations=['import'], limits=dict(max_edges=1))
                def post(endpoint, payload):
                    return urlopen(Request(address + endpoint, encoded(payload), headers={'Content-Type': 'application/json'}))
                try:
                    with post('/api/query', request) as response: page = json.loads(response.read())
                    self.assertIsNotNone(page['cursor']); self.assertEqual(len(page['rows']), 1)
                    handle = {key: page['rows'][0]['site'][key] for key in ('id', 'path', 'range', 'source_sha256')}
                    (root / 'helper.py').write_text('def leaf():\n    return 2\n')
                    updated = StructuralIndex(root, out).refresh(['main.py', 'other.py', 'helper.py'], git_base=base)
                    self.assertEqual(updated['status'], 'ready', updated); self.assertNotEqual(updated['generation'], page['generation'])
                    for endpoint, values, code in (('/api/query', dict(request, cursor=page['cursor']), 400),
                            ('/api/source', dict(generation=page['generation'], handle=handle), 409)):
                        with self.assertRaises(HTTPError) as caught: post(endpoint, values)
                        self.assertEqual(caught.exception.code, code); caught.exception.close()
                finally: server.shutdown(); thread.join()

    def test_captured_source_exact_handles_utf8_redaction_and_byte_caps(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        import hashlib
        from repo_graph.analysis import StructuralIndex
        from repo_graph import analysis_native
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            secret = 'sk-' + 'SyntheticOnlyToken0123456789'
            text = f'def café():\n    return unknown("{secret}", "é")\n'
            text += 'def long():\n    return "' + 'é' * 5000 + '"\n'
            text += 'def private():\n    return "-----BEGIN PRIVATE KEY-----"\n'
            (root / 'main.py').write_text(text)
            index = StructuralIndex(root, out); receipt = index.refresh(['main.py'])
            self.assertEqual(receipt['status'], 'ready', receipt)
            definitions, sites = index.read_facts('definitions'), index.read_facts('sites')
            def handle(row):
                return {key: row[key] for key in ('id', 'path', 'range')} | {'source_sha256': row['provenance']['source_sha256']}
            declaration = next(row for row in definitions if row['name'] == 'café')
            site = next(row for row in sites if row['role'] == 'call')
            large = next(row for row in definitions if row['name'] == 'long')
            private = next(row for row in definitions if row['name'] == 'private')
            engine = search.Search(out)
            self.addCleanup(engine.close)
            before = (out / 'search.db').read_bytes()
            (root / 'main.py').unlink()  # Inspection remains captured, even without live source.
            forbidden = AssertionError('Source inspection must not scan, parse, use Git, or initialize a model')
            with patch.object(SourceRoot, 'read', side_effect=forbidden), \
                    patch.object(StructuralIndex, 'refresh', side_effect=forbidden), \
                    patch.object(analysis_native, 'backend', side_effect=forbidden), \
                    patch.object(search.Embeddings, '__init__', side_effect=forbidden), \
                    patch.object(subprocess, 'run', side_effect=forbidden):
                for row in (declaration, site):
                    response = search.captured_source(engine, {'generation': receipt['generation'], 'handle': handle(row)})
                    self.assertEqual(response['handle'], handle(row))
                    self.assertEqual(response['identities']['structural_generation'], receipt['generation'])
                    self.assertEqual(response['source_sha256'], hashlib.sha256(text.encode()).hexdigest())
                    self.assertEqual(response['file_bytes'], len(text.encode()))
                    self.assertEqual(response['range'], row['range'])
                    self.assertEqual(response['raw_digest'], hashlib.sha256(row['text'].encode()).hexdigest())
                    self.assertEqual(response['evidence_kind'], 'static_syntax')
                    self.assertTrue(response['redacted']); self.assertFalse(response['truncated'])
                    self.assertIn('[redacted]', response['text'])
                    self.assertNotIn(secret, search._evidence_encoded(response).decode())
                    self.assertLessEqual(len(search._evidence_encoded(response)), 32768)
                self.assertEqual(response['kind'], 'callsite')
                self.assertEqual(response['certainty'], 'unresolved')
                self.assertFalse(response['targets_exhaustive'])
                self.assertEqual(response['reason'], site['reason'])
                default = search.captured_source(engine, {'generation': receipt['generation'], 'handle': handle(large)})
                self.assertTrue(default['truncated']); self.assertEqual(default['budgets']['max_excerpt_bytes'], 4096)
                for maximum in (0, 1, 24, 4096, 8192):
                    response = search.captured_source(engine, {'generation': receipt['generation'],
                        'handle': handle(large), 'max_excerpt_bytes': maximum})
                    raw = text.encode()[response['range']['start_byte']:response['range']['end_byte']]
                    self.assertEqual(response['text'], raw.decode('utf-8'))
                    self.assertEqual(response['raw_digest'], hashlib.sha256(raw).hexdigest())
                    self.assertLessEqual(len(response['text'].encode()), maximum)
                    self.assertFalse(response['redacted']); self.assertTrue(response['truncated'])
                with self.assertRaisesRegex(ValueError, 'Private key'):
                    search.captured_source(engine, {'generation': receipt['generation'], 'handle': handle(private)})
            self.assertEqual((out / 'search.db').read_bytes(), before)

    def test_captured_source_refuses_forgery_stale_capture_and_stored_digest_drift(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        from repo_graph.analysis import StructuralIndex
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            (root / 'main.py').write_text('def start():\n    missing()\n')
            index = StructuralIndex(root, out); receipt = index.refresh(['main.py'])
            self.assertEqual(receipt['status'], 'ready', receipt)
            row = next(index.read_facts('sites'))
            handle = {key: row[key] for key in ('id', 'path', 'range')} | {'source_sha256': row['provenance']['source_sha256']}
            request = {'generation': receipt['generation'], 'handle': handle}
            engine = search.Search(out); self.addCleanup(engine.close)
            invalid = [[], dict(request, extra=True), dict(request, max_excerpt_bytes=True),
                dict(request, max_excerpt_bytes=-1), dict(request, max_excerpt_bytes=8193),
                dict(request, handle=dict(handle, extra=True)),
                dict(request, handle=dict(handle, id='unknown.py:0:1:call')),
                dict(request, handle=dict(handle, path='other.py')),
                dict(request, handle=dict(handle, path='../main.py')),
                dict(request, handle=dict(handle, source_sha256='0' * 64)),
                dict(request, handle=dict(handle, range=dict(handle['range'], start_line=999))),
                dict(request, handle=dict(handle, range=dict(handle['range'], start_byte=True)))]
            for values in invalid:
                with self.subTest(values=values), self.assertRaises(ValueError): search.captured_source(engine, values)
            with self.assertRaises(search.CapturedSourceConflict):
                search.captured_source(engine, dict(request, generation='0' * 64))
            with self.assertRaises(InterruptedError): search.captured_source(engine, request, cancel=lambda: True)
            clock = [0.0]; original_connect = engine.connect
            def slow_open(**kwargs):
                db = original_connect(**kwargs); clock[0] = .6; return db
            with patch.object(search.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(engine, 'connect', side_effect=slow_open), self.assertRaises(InterruptedError):
                search.captured_source(engine, request)
            # The handle must still agree with both the stored member and file digest.
            with closing(search.connect(out)) as db, db:
                stored = json.loads(db.execute('SELECT data FROM structural_sites WHERE id=?', (row['id'],)).fetchone()[0])
                stored['provenance']['source_sha256'] = '0' * 64
                db.execute('UPDATE structural_sites SET data=? WHERE id=?', (json.dumps(stored), row['id']))
            with self.assertRaisesRegex(ValueError, 'source handle'):
                search.captured_source(engine, request)
            with closing(search.connect(out)) as db, db:
                db.execute('UPDATE structural_sites SET data=? WHERE id=?', (json.dumps(row), row['id']))
                record = json.loads(db.execute('SELECT record FROM structural_files WHERE path=?', ('main.py',)).fetchone()[0])
                record['sha256'] = '1' * 64
                db.execute('UPDATE structural_files SET record=? WHERE path=?', (json.dumps(record), 'main.py'))
            with self.assertRaisesRegex(ValueError, 'source handle'):
                search.captured_source(engine, request)

    def test_captured_source_http_acceptance_and_refusal_guards(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        from repo_graph.analysis import StructuralIndex
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            (root / 'main.py').write_text('def start():\n    missing()\n')
            index = StructuralIndex(root, out); receipt = index.refresh(['main.py'])
            self.assertEqual(receipt['status'], 'ready', receipt)
            row = next(index.read_facts('definitions'))
            handle = {key: row[key] for key in ('id', 'path', 'range')} | {'source_sha256': row['provenance']['source_sha256']}
            payload = dict(generation=receipt['generation'], handle=handle)
            with create_server(search.Search(out)) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
                url = f'http://127.0.0.1:{server.server_port}/api/source'
                def post(values, headers=None):
                    return urlopen(Request(url, values if isinstance(values, bytes) else search._evidence_encoded(values),
                        headers={'Content-Type': 'application/json', **(headers or {})}))
                try:
                    with post(payload) as response:
                        raw = response.read(); observed = json.loads(raw)
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    self.assertEqual(observed['handle'], handle)
                    self.assertEqual(observed['text'], row['text'])
                    self.assertLessEqual(len(raw), 32768)
                    failures = [
                        (dict(payload, generation='0' * 64), {}, 409),
                        (dict(payload, handle=dict(handle, source_sha256='f' * 64)), {}, 400),
                        (dict(payload, max_excerpt_bytes=8193), {}, 400),
                        (dict(payload, unknown=True), {}, 400),
                        (payload, {'X-Repo-Graph-Output': '0' * 64}, 409),
                        (payload, {'Origin': 'https://untrusted.invalid'}, 403),
                        (payload, {'Host': 'untrusted.invalid'}, 403),
                        (payload, {'Content-Type': 'text/plain'}, 415),
                        (b' ' * 8193, {}, 400),
                        (b'{"generation":"x","generation":"y","handle":{}}', {}, 400)]
                    for values, headers, code in failures:
                        with self.subTest(code=code, headers=headers), self.assertRaises(HTTPError) as caught:
                            post(values, headers)
                        self.assertEqual(caught.exception.code, code); caught.exception.close()
                    with patch.object(search, 'captured_source', side_effect=InterruptedError('synthetic stop')):
                        with self.assertRaises(HTTPError) as caught: post(payload)
                        self.assertEqual(caught.exception.code, 503); caught.exception.close()
                finally: server.shutdown(); thread.join()

    def test_function_evidence_preserves_multilanguage_ranges_and_nested_members(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        import hashlib
        from repo_graph.analysis import StructuralIndex
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'evaluations/code-understanding/fixtures.json').read_text())
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            originals = Path(__file__).resolve().parents[1]
            for row in fixture['files']:
                target = root / row['path']; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((originals / row['path']).read_bytes())
            index = StructuralIndex(root, out)
            receipt = index.refresh(fixture['files'])
            self.assertEqual(receipt['status'], 'ready', receipt)
            for language in ('python', 'go', 'javascript', 'typescript'):
                path = next(row['path'] for row in fixture['files'] if row['language'] == language and '/main.' in row['path'])
                response = search.Search(out).run('café', kind='functions', mode='keyword', prefix=path, limit=50)
                self.assertEqual(response['identities']['structural_generation'], receipt['generation'])
                self.assertTrue(any(member['name'] == 'café' for row in response['results'] for member in row['members']))
                for row in response['results']:
                    raw = (root / row['path']).read_bytes(); span = row['range']
                    self.assertEqual(row['file_sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(row['text'].encode(), raw[span['start_byte']:span['end_byte']])
                    self.assertEqual(row['raw_digest'], hashlib.sha256(row['text'].encode()).hexdigest())
                    self.assertEqual(row['evidence_kind'], 'static_syntax')
            prefix = 'tests/fixtures/code-understanding/python/main.py'
            response = search.Search(out).run('shadow', kind='functions', mode='keyword', prefix=prefix, limit=50)
            merged = [row for row in response['results'] if any(member['name'] == 'shadow' for member in row['members'])]
            self.assertEqual(len(merged), 1)
            self.assertEqual((merged[0]['range']['start_byte'], merged[0]['range']['end_byte']), (374, 456))
            self.assertEqual({member['name'] for member in merged[0]['members']}, {'shadow', 'shadow.local'})
            self.assertEqual(response['counts']['returned_symbol_handles'],
                len({member['symbol_id'] for row in response['results'] for member in row['members']}))
            tiny = search.Search(out).run('shadow', kind='functions', mode='keyword', prefix=prefix,
                                         limits=search.EvidenceLimits(max_entities=1))
            self.assertTrue(tiny['truncated'])
            self.assertLessEqual(tiny['counts']['returned_symbol_handles'], 1)
            for row in tiny['results']:
                self.assertFalse('shadow' in {member['name'] for member in row['members']} and
                                 'shadow.local' not in {member['name'] for member in row['members']})
            unsupported = search.Search(out).run('Worker.run', kind='functions', mode='keyword',
                prefix='tests/fixtures/code-understanding/typescript/main.ts')
            self.assertFalse(any(member['name'] == 'Worker.run' for row in unsupported['results'] for member in row['members']))

    def test_function_evidence_cli_api_caps_invalid_limits_and_cancellation(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        from repo_graph.analysis import StructuralIndex
        from repo_graph.cli import main
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            (root / 'main.py').write_text('def oldFunction(): pass\n')
            self.assertEqual(StructuralIndex(root, out).refresh(['main.py'])['status'], 'ready')
            limits = {'max_response_bytes': 4096, 'max_excerpt_bytes': 16, 'max_entities': 1}
            forbidden = AssertionError('Keyword function evidence must not initialize a model')
            with patch.object(search.Embeddings, '__init__', side_effect=forbidden):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    self.assertEqual(main(['search', str(out), 'oldFunction', '--kind', 'functions',
                        '--mode', 'keyword', '--limits', json.dumps(limits)]), 0)
                command = json.loads(buffer.getvalue())
                self.assertLessEqual(len(buffer.getvalue().strip().encode()), limits['max_response_bytes'])
                self.assertTrue(command['truncated'])
                self.assertLessEqual(sum(len(row['text'].encode()) for row in command['results']), 16)
                with redirect_stdout(io.StringIO()), patch('sys.stderr', new=io.StringIO()):
                    self.assertEqual(main(['search', str(out), 'oldFunction', '--kind', 'functions',
                        '--mode', 'semantic', '--limits', '[]']), 1)
                with create_server(search.Search(out)) as server:
                    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
                    url = 'http://127.0.0.1:' + str(server.server_port) + '/api/search'
                    def post(values):
                        return urlopen(Request(url, json.dumps(values).encode(), headers={'Content-Type': 'application/json'}))
                    try:
                        with post({'query': 'oldFunction', 'kind': 'functions', 'mode': 'keyword', 'limits': limits}) as response:
                            raw = response.read(); endpoint = json.loads(raw)
                        self.assertLessEqual(len(raw), limits['max_response_bytes'])
                        self.assertEqual(endpoint['identities'], command['identities'])
                        self.assertEqual(endpoint['results'], command['results'])
                        for invalid in ({'max_entities': True}, {'max_excerpt_bytes': -1},
                                        {'max_response_bytes': 1}, {'unknown_budget': 1}, []):
                            with self.subTest(limits=invalid), self.assertRaises(HTTPError) as caught:
                                post({'query': 'oldFunction', 'kind': 'functions', 'mode': 'keyword', 'limits': invalid})
                            self.assertEqual(caught.exception.code, 400); caught.exception.close()
                    finally: server.shutdown(); thread.join()
            before = (out / 'search.db').read_bytes()
            def cancel(): raise InterruptedError('synthetic function query cancellation')
            with self.assertRaises(InterruptedError):
                search.Search(out).run('oldFunction', kind='functions', mode='keyword', cancel=cancel)
            self.assertEqual((out / 'search.db').read_bytes(), before)
            baseline = search.Search(out).run('oldFunction', kind='functions', mode='keyword')
            cancelled, external_calls = threading.Event(), []
            excerpt = search._function_excerpt
            def cancel_after_only_excerpt(*args, **kwargs):
                result = excerpt(*args, **kwargs)
                cancelled.set()
                return result
            class RecordingRanker:
                def rank(self, query, rows):
                    external_calls.append('rank')
                    return rows, {}
            for ranker in (None, RecordingRanker()):
                cancelled.clear()
                with self.subTest(reranker=ranker is not None), \
                        patch.object(search, '_function_excerpt', side_effect=cancel_after_only_excerpt):
                    stopped = search.Search(out).run('oldFunction', kind='functions', mode='keyword',
                        cancel=cancelled.is_set, reranker=ranker)
                self.assertTrue(stopped['truncated'])
                self.assertEqual(stopped['stop_reason'], 'cancelled')
                self.assertEqual(external_calls, [])
                self.assertEqual(stopped['results'], baseline['results'])
            class MutatingRanker:
                def rank(self, query, rows):
                    external_calls.append('mutate')
                    rows[0].setdefault('range', {})['start_byte'] = 1234
                    member = rows[0].setdefault('members', [{'range': {}}])[0]
                    member['symbol_id'] = 'forged-symbol'
                    member['range']['end_byte'] = 9999
                    return rows, {}
            preserved = search.Search(out).run('oldFunction', kind='functions', mode='keyword',
                reranker=MutatingRanker())
            self.assertEqual(external_calls, ['mutate'])
            self.assertEqual(preserved['results'], baseline['results'])
            self.assertEqual(preserved['identities'], baseline['identities'])
            self.assertEqual(preserved['counts'], baseline['counts'])
            self.assertEqual((out / 'search.db').read_bytes(), before)

    def test_serve_missing_cached_backend_keeps_keyword_functions_available(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        from repo_graph.analysis import StructuralIndex
        from repo_graph.cli import main
        class FakeEmbedding:
            name = 'synthetic-evidence-v1'
            packed = staticmethod(lambda value: value)
            def passages(self, texts): return [b'fresh-vector' for _ in texts]
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'; root.mkdir()
            (root / 'main.py').write_text('def oldFunction(): pass\n')
            self.assertEqual(StructuralIndex(root, out).refresh(['main.py'])['status'], 'ready')
            search.embed_index(out, FakeEmbedding(), kind='functions')
            served = []
            def captured_serve(engine, *args, **kwargs):
                self.assertIsNone(engine.embedder)
                result = engine.run('oldFunction', kind='functions', mode='keyword')
                self.assertTrue(result['results'])
                observed = search.index_status(out, backend_available=False)
                self.assertTrue(observed['function_evidence']['semantic_artifact_ready'])
                self.assertFalse(observed['function_evidence']['semantic_query_available'])
                served.append(observed)
            for failure in (RuntimeError('synthetic backend absent'), ValueError('synthetic model cache absent')):
                with self.subTest(error=type(failure).__name__), \
                        patch.object(search.Embeddings, '__init__', side_effect=failure), \
                        patch('repo_graph.server.serve', side_effect=captured_serve), \
                        redirect_stdout(io.StringIO()), patch('sys.stderr', new=io.StringIO()):
                    self.assertEqual(main(['serve', str(out)]), 0)
                    self.assertEqual(main(['search', str(out), 'oldFunction', '--kind', 'functions', '--mode', 'semantic']), 1)
            self.assertEqual(len(served), 2)

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
                ('contradictory_previous_generation', lambda row: row.update(previous_generation='f' * 64)),
                ('contradictory_nested_previous_generation', lambda row: row['receipt'].update(previous_generation='f' * 64)),
                ('contradictory_published_coverage_generation', lambda row: row['receipt'].update(published_coverage_generation='f' * 64)),
                ('nested_current_generation_cannot_rescue_foreign_prior', lambda row: (
                    row.update(previous_generation='f' * 64),
                    row['receipt'].update(previous_generation='f' * 64,
                        published_coverage_generation='f' * 64, generation=ready['generation']))),
                ('contradictory_reason', lambda row: (
                    row.update(reason='source_changed_before_publication'),
                    row['receipt'].update(reason='cancelled'))),
            ]
            try:
                for name, mutate in nested_mutations:
                    with self.subTest(nested_failure=name):
                        row = json.loads(json.dumps(failure_record)); mutate(row)
                        attempt.write_text(json.dumps(row))
                        refused = search.index_status(out, backend_available=True)
                        if name == 'nested_current_generation_cannot_rescue_foreign_prior':
                            self.assertEqual(refused['status'], 'ok')
                            current = refused['structural']
                            self.assertEqual(current['attempt_attribution'], 'unrelated_generation')
                            self.assertEqual(current['state'], 'ready')
                            self.assertEqual(current['freshness'], 'unknown')
                            self.assertTrue(current['artifact_ready'])
                            self.assertEqual(current['identities'], baseline['structural']['identities'])
                            continue
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
