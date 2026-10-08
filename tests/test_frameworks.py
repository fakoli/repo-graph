"""Shared syntax handoff checks; framework resolution is qualified separately."""
import copy
import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_graph import analysis_native as native


AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))


@unittest.skipUnless(AVAILABLE, 'Optional analysis backend')
class FrameworkSyntaxTests(unittest.TestCase):
    def test_identity_mutation_and_excluded_callback_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/django-framework-inputs.json').read_text())
        original = {record['path']: (root / manifest['fixture_root'] / record['path']).read_bytes()
                    for record in manifest['synthetic_inventory'] if record['path'].endswith('.py')}
        route = b'from django.urls import path\nfrom .views import homepage\nurlpatterns = [path("x/", homepage)]\n'
        mutations = {
            'importer_wildcard': {'urls.py': route.replace(b'urlpatterns =', b'from .evil import *\nurlpatterns =')},
            'api_wildcard': {'django/urls/conf.py': original['django/urls/conf.py'] + b'\nfrom .evil import *\n'},
            'callback_wildcard': {'views.py': original['views.py'] + b'\nfrom .evil import *\n'},
            'subscript_write': {'urls.py': route + b'urlpatterns[0] = None\n'},
            'subscript_delete': {'urls.py': route + b'del urlpatterns[0]\n'},
            'binding_delete': {'urls.py': route + b'del urlpatterns\n'},
            'alias_mutation': {'urls.py': route + b'other = urlpatterns\nother.clear()\n'},
            'argument_escape': {'urls.py': route + b'mutate(urlpatterns)\n'},
            'package_attribute': {'urls.py': b'from django.urls import path\nfrom . import views\nurlpatterns = [path("x/", views.homepage)]\n'},
        }
        for label, changed in mutations.items():
            with self.subTest(boundary=label):
                blobs = original | {'urls.py': route, 'evil.py': b'def path(*args): pass\n'} | changed
                files = [native.collect_file(dict(path=path, language='python', content=raw)) for path, raw in blobs.items()]
                ordinary = native.resolve_collected(files)['facts']
                facts = native.resolve_collected(files, framework_context=manifest['source_admission']['frozen_contexts']['synthetic'])['facts']
                rows = [row for row in facts['sites'] if row['path'] == 'urls.py' and row['role'].startswith('framework')]
                self.assertEqual(len(rows), 1, rows)
                self.assertEqual((rows[0]['family'], rows[0]['targets'], rows[0]['certainty']),
                                 ('framework_boundary', [], 'unresolved'))
                self.assertEqual([row for row in facts['sites'] if not row['role'].startswith('framework')], ordinary['sites'])
        # The reviewed finite export exception permits earlier wildcard imports,
        # followed by the final explicit Manager import; it does not resolve '*'.
        files = [native.collect_file(dict(path=path, language='python', content=raw)) for path, raw in original.items()]
        facts = native.resolve_collected(files, framework_context=manifest['source_admission']['frozen_contexts']['synthetic'])['facts']
        self.assertTrue(any(row['role'] == 'framework' and row['relation_kind'] == 'django_orm_get_queryset' for row in facts['sites']))

    def test_framework_availability_is_captured_per_generation(self):
        from repo_graph.analysis import StructuralIndex
        from repo_graph.analysis_queries import SQLSnapshot
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/django-framework-inputs.json').read_text())
        context = manifest['source_admission']['frozen_contexts']['synthetic']
        with tempfile.TemporaryDirectory() as scratch:
            source, output = Path(scratch) / 'source', Path(scratch) / 'out'; source.mkdir()
            (source / 'plain.py').write_text('def plain(): pass\n')
            for enrollment in (None, context):
                index = StructuralIndex(source, output, framework_context=enrollment)
                self.assertEqual(index.refresh(['plain.py'])['status'], 'ready')
                with SQLSnapshot(output) as snapshot:
                    page = snapshot.query(operation='framework')
                self.assertEqual(page['rows'], [])
                self.assertEqual(page['coverage']['status'], 'unavailable' if enrollment is None else 'enabled')
                self.assertEqual(page['total_count'], {'value': None, 'kind': 'unavailable'} if enrollment is None else {'value': 0, 'kind': 'exact'})
                self.assertEqual(page['stop_reason'], 'framework_not_enrolled' if enrollment is None else None)

    def test_inheritance_discovery_charges_work_and_observes_deadline_and_cancel(self):
        import time
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/django-framework-inputs.json').read_text())
        context = manifest['source_admission']['frozen_contexts']['synthetic']
        raw = ('class C0: pass\n' + ''.join(f'class C{i}(C{i-1}, C{i-1}): pass\n' for i in range(1, 17))).encode()
        def collected():
            return native.collect_file(dict(path='app.py', language='python', content=raw))
        file = collected()
        limit = file.counts['nodes'] + 8
        result = native.resolve_collected([file], framework_context=context, budget=native.Budget(max_nodes=limit))
        self.assertNotEqual(result['status'], 'complete')
        self.assertEqual(result['stop_reason'], 'node_budget_exceeded')
        node = native.Work.node
        for reason in ('deadline_exceeded', 'cancelled'):
            cancelled = [False]
            def controlled(work):
                if work.nodes >= limit:
                    if reason == 'cancelled': cancelled[0] = True
                    else: work.started = time.perf_counter() - 1
                return node(work)
            with self.subTest(reason=reason), patch.object(native.Work, 'node', controlled):
                result = native.resolve_collected([collected()], framework_context=context,
                    budget=native.Budget(timeout_seconds=0.5), cancel=lambda: cancelled[0])
            self.assertNotEqual(result['status'], 'complete')
            self.assertEqual(result['stop_reason'], reason)

    def test_registration_context_rebinds_without_recollecting_and_is_opt_in(self):
        from repo_graph.analysis import StructuralIndex
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/django-framework-inputs.json').read_text())
        context = manifest['source_admission']['frozen_contexts']['synthetic']
        with tempfile.TemporaryDirectory() as scratch:
            source, output = Path(scratch) / 'source', Path(scratch) / 'out'; source.mkdir()
            paths = [record['path'] for record in manifest['synthetic_inventory']]
            for path in paths:
                target = source / path; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((root / manifest['fixture_root'] / path).read_bytes())
            plain = StructuralIndex(source, output)
            old = plain.refresh(paths); self.assertEqual(old['status'], 'ready', old)
            self.assertFalse(any(row['role'].startswith('framework') for row in plain.read_facts('sites')))
            registered = StructuralIndex(source, output, framework_context=context)
            with patch.object(native, 'backend', side_effect=AssertionError('Context rebind reparsed source')):
                ready = registered.refresh(paths)
            self.assertEqual(ready['status'], 'ready', ready)
            self.assertEqual(ready['resources']['changed_files_collected'], 0)
            self.assertEqual(ready['resources']['unchanged_source_collections_reused'], sum(path.endswith('.py') for path in paths))
            self.assertNotEqual(ready['config_identity'], old['config_identity'])
            self.assertTrue(any(row['role'] == 'framework' for row in registered.read_facts('sites')))
            removed = StructuralIndex(source, output).refresh(paths)
            self.assertEqual(removed['status'], 'ready', removed)
            self.assertFalse(any(row['role'].startswith('framework') for row in plain.read_facts('sites')))
            for mutation in ('escape', 'foreign_root', 'gold', 'enabled', 'conflicting_snapshot'):
                value = copy.deepcopy(context)
                if mutation == 'escape': value['source_roots'][0]['source_prefix'] = '../'
                elif mutation == 'foreign_root': value['dependency']['source_root_id'] = 'other'
                elif mutation == 'gold': value['targets'] = ['oracle']
                elif mutation == 'enabled': value['enabled'] = 1
                else: value['dependency']['revision'] = '0' * 64
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    StructuralIndex(source, output, framework_context=value)
            from repo_graph.cli import main
            config = Path(scratch) / 'enrollment.json'; config.write_text(json.dumps(context))
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(['analyze', str(source), '--output', str(output), '--framework-context', str(config)]), 0)
            self.assertEqual(json.loads(buffer.getvalue())['framework_enrollment'], context)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(['query', str(output), '--operation', 'framework', '--family', 'framework',
                                      '--relation-kind', 'django_route']), 0)
            self.assertEqual(len(json.loads(buffer.getvalue())['rows']), 4)
            for invalid in ('{"enabled":true,"enabled":false}', '{"enabled":NaN}', '[]'):
                config.write_text(invalid)
                with redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
                    self.assertEqual(main(['analyze', str(source), '--output', str(output), '--framework-context', str(config)]), 1)

    def test_shared_framework_pages_evidence_filters_and_held_snapshots(self):
        from repo_graph.analysis import StructuralIndex
        from repo_graph.analysis_queries import Queries, SQLSnapshot, Limits, encoded
        from repo_graph import search
        from repo_graph.source import SourceRoot
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/django-framework-inputs.json').read_text())
        context = manifest['source_admission']['frozen_contexts']['synthetic']
        with tempfile.TemporaryDirectory() as scratch:
            source, output = Path(scratch) / 'source', Path(scratch) / 'output'
            source.mkdir()
            inventory = [record['path'] for record in manifest['synthetic_inventory']]
            for path in inventory:
                target = source / path; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((root / manifest['fixture_root'] / path).read_bytes())
            index = StructuralIndex(source, output, framework_context=context)
            receipt = index.refresh(inventory)
            self.assertEqual(receipt['status'], 'ready', receipt)
            with Queries(output) as queries:
                result = queries.run({'operation': 'framework'})
                page = result
                rows = list(result['rows'])
                while page['cursor']:
                    page = queries.run({'operation': 'framework', 'cursor': page['cursor']})
                    rows.extend(page['rows'])
                self.assertFalse(page['truncated'], page['stop_reason'])
                result['rows'] = rows
                facts = list(index.read_facts('sites'))
                expected = [site for site in facts if site['role'].startswith('framework')]
                self.assertEqual(len(result['rows']), len(expected))
                self.assertTrue(any(row['family'] == 'framework_boundary' and row['target'] is None for row in result['rows']))
                self.assertEqual([row['site']['id'] for row in result['rows']], [site['id'] for site in sorted(expected,
                    key=lambda row: (row['path'], row['range']['start_byte'], row['range']['end_byte'], row['family'], row['relation_kind'], row['id']))])
                supported = queries.run({'operation': 'framework', 'families': ['framework'], 'kinds': ['django_route']})
                self.assertEqual(len(supported['rows']), 4)
                envelope = {key: result[key] for key in ('generation', 'repository_identity', 'source_identity', 'analyzer_identity', 'config_identity')}
                handles = [row['site'] for row in result['rows']]
                handles += [{key: item[key] for key in ('id', 'path', 'range', 'source_sha256')}
                            for row in result['rows'] for item in row['evidence'] if 'id' in item]
                engine = search.Search(output)
                try:
                    with patch.object(SourceRoot, 'read', side_effect=AssertionError('Source read during captured inspection')), \
                            patch.object(native, 'backend', side_effect=AssertionError('Parser during captured inspection')):
                        for handle in handles:
                            handle = {key: handle[key] for key in ('id', 'path', 'range', 'source_sha256')}
                            response = search.captured_source(engine, dict(generation=receipt['generation'], handle=handle, max_excerpt_bytes=64))
                            self.assertLessEqual(len(response['text'].encode('utf-8')), 64)
                    for changes in ({'families': ['invented']}, {'kinds': ['invented']}, {'operation': 'call', 'families': ['framework']}):
                        with self.assertRaises(ValueError): queries.run(dict(operation='framework') | changes)
                finally: engine.close()
                request = dict(operation='framework', limits=dict(max_edges=1))
                page = queries.run(request)
                with self.assertRaises(ValueError): queries.run(request | dict(cursor=page['cursor'], families=['framework']))
                old_handle = {key: page['rows'][0]['site'][key] for key in ('id', 'path', 'range', 'source_sha256')}
                (source / 'views.py').write_bytes((source / 'views.py').read_bytes() + b'\n# edited body source\n')
                refreshed = index.refresh(inventory)
                self.assertEqual(refreshed['status'], 'ready', refreshed)
                self.assertNotEqual(refreshed['generation'], receipt['generation'])
                next_page = queries.run(request | dict(cursor=page['cursor']))
                self.assertEqual(next_page['generation'], receipt['generation'])
                self.assertNotEqual(next_page['rows'][0]['site']['id'], old_handle['id'])
                engine = search.Search(output)
                try:
                    with self.assertRaises(search.CapturedSourceConflict):
                        search.captured_source(engine, dict(generation=receipt['generation'], handle=old_handle))
                finally: engine.close()
            with SQLSnapshot(output) as snapshot:
                entities = snapshot.query(operation='framework', limits=Limits(max_entities=1))
                self.assertEqual(entities['stop_reason'], 'entity_budget_exceeded')
                work = snapshot.query(operation='framework', limits=Limits(max_examined_relationships=1))
                self.assertLessEqual(work['examined_relationships'], 1)
                edges = snapshot.query(operation='framework', limits=Limits(max_edges=1))
                self.assertLessEqual(edges['returned_edges'], 1)
                self.assertEqual(snapshot.query(operation='framework', cancel=lambda: True)['stop_reason'], 'cancelled')
                byte_page = snapshot.query(operation='framework', limits=Limits(max_response_bytes=1600))
                self.assertLessEqual(len(encoded(byte_page)), 1600)
                self.assertEqual(byte_page['stop_reason'], 'response_byte_budget_exceeded')

    def test_frozen_synthetic_framework_occurrences_and_lookalikes(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/django-framework-inputs.json').read_text())
        files = [native.collect_file(dict(path=record['path'], language='python',
                 content=(root / manifest['fixture_root'] / record['path']).read_bytes()))
                 for record in manifest['synthetic_inventory']]
        ordinary = native.resolve_collected(files)['facts']
        self.assertFalse(any(site['role'].startswith('framework') for site in ordinary['sites']))
        facts = native.resolve_collected(files,
            framework_context=manifest['source_admission']['frozen_contexts']['synthetic'])['facts']
        definitions = {definition['id']: definition for definition in facts['definitions']}
        for case in manifest['cases']:
            if case['source_kind'] != 'synthetic':
                continue
            with self.subTest(case=case['id']):
                origin = case['origin']
                rows = [site for site in facts['sites'] if site['role'].startswith('framework')
                        and site['path'] == origin['path'] and all(site['range'][key] == origin['range'][key]
                        for key in ('start_byte', 'end_byte'))]
                self.assertEqual(len(rows), case['expected']['source_row_count'])
                if not rows:
                    continue
                row = rows[0]
                self.assertEqual(row['family'], case['relation_family'])
                self.assertEqual(row['certainty'], case['expected']['certainty'])
                self.assertEqual(row['framework_identity_asserted'], case['expected']['framework_identity_asserted'])
                self.assertEqual(len(row['targets']), case['expected']['target_cardinality'])
                for target, expected in zip(row['targets'], case['expected']['targets']):
                    definition = definitions[target]
                    self.assertEqual((definition['path'], definition['name']), (expected['path'], expected['name']))
                    for key in ('start_byte', 'end_byte'):
                        self.assertEqual(definition['range'][key], expected['range'][key])
        for site in facts['sites']:
            if site['role'] in ('call', 'reference'):
                old = next(row for row in ordinary['sites'] if row['id'] == site['id'])
                self.assertEqual(site, old)

    def test_admitted_syntax_roundtrip_preserves_arguments_owners_and_fact_limits(self):
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / 'evaluations/code-understanding/django-framework-inputs.json'
        manifest = json.loads(manifest_path.read_text())
        review = json.loads((root / 'evaluations/code-understanding/django-framework-review.json').read_text())
        self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), review['input_manifest']['sha256'])
        self.assertEqual(review['status'], 'admitted_frozen_source_key')
        collected = {}
        for record in manifest['synthetic_inventory']:
            raw = (root / manifest['fixture_root'] / record['path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), record['sha256'])
            file = native.collect_file(dict(path=record['path'], language='python', content=raw))
            encoded = file.to_json()
            decoded = native.CollectedFile.from_json(encoded, file.record, hashlib.sha256(encoded).hexdigest())
            self.assertEqual(decoded.to_json(), encoded)
            self.assertEqual(decoded.collected_fact_count, file.collected_fact_count)
            collected[file.path] = file
        urls = collected['urls.py']; syntax = urls.syntax_metadata
        assignment = next(row for row in syntax['python_assignments'] if row['name'] == 'urlpatterns')
        self.assertEqual((assignment['scope'], assignment['conditional'], assignment['right']['type']), (0, False, 'list'))
        self.assertEqual(len(assignment['elements']), 12)
        calls = {row['site']: row for row in syntax['calls']}
        first = assignment['elements'][0]
        call = calls[f'urls.py:{first["start_byte"]}:{first["end_byte"]}:call']
        self.assertEqual([row['expression']['spelling'] for row in call['arguments']], ['"café/"', 'home', '"home"'])
        self.assertEqual([row['name'] for row in call['arguments']], ['', '', 'name'])
        command = collected['management/commands/report.py']
        declarations = command.syntax_metadata['python_declarations']
        definitions = {row['id']: row for row in command.definitions}
        cls = next(row for row in declarations if definitions[row['id']]['kind'] == 'class')
        method = next(row for row in declarations if definitions[row['id']]['name'] == 'Command.handle')
        self.assertEqual(len(cls['bases']), 1)
        self.assertFalse(cls['decorated']); self.assertFalse(method['conditional'])
        self.assertEqual(command.scopes[method['scope']].owner, cls['id'])
        conditional = native.collect_file(dict(path='conditional.py', language='python', content=
            b'if enabled:\n    import django.urls as routes\nfrom . import *\n'))
        self.assertEqual([row['conditional'] for row in conditional.syntax_metadata['import_contexts']], [True, False])
        self.assertEqual(conditional.imports[-1]['symbol'], '*')
        self.assertGreater(urls.collected_fact_count, len(urls.definitions) + len(urls.imports))
        budget = native.Budget(max_facts=urls.collected_fact_count - 1)
        with self.assertRaisesRegex(native.StopScan, 'fact_budget_exceeded'):
            native.resolve_collected([urls], budget=budget)
        with self.assertRaisesRegex(native.StopScan, 'fact_budget_exceeded'):
            encoded = urls.to_json()
            native.CollectedFile.from_json(encoded, urls.record, hashlib.sha256(encoded).hexdigest(), budget)
        for mutation in ('foreign_site', 'targets', 'argument_range', 'foreign_scope'):
            with self.subTest(mutation=mutation):
                payload = copy.deepcopy(urls.payload())
                row = payload['syntax_metadata']['calls'][0]
                if mutation == 'foreign_site': row['site'] = 'foreign:1:2:call'
                elif mutation == 'targets': row['targets'] = ['invented:1:2']
                elif mutation == 'foreign_scope': payload['syntax_metadata']['python_assignments'][0]['scope'] = 9999
                else: row['arguments'][0]['expression']['start_byte'] = 0
                encoded = json.dumps(payload).encode()
                with self.assertRaises(ValueError):
                    native.CollectedFile.from_json(encoded, urls.record, hashlib.sha256(encoded).hexdigest())


if __name__ == '__main__':
    unittest.main()
