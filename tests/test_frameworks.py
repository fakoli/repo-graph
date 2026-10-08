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
    @staticmethod
    def odoo_inputs():
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/odoo-framework-inputs.json').read_text())
        blobs = {row['path']: (root / manifest['fixture_root'] / row['path']).read_bytes()
                 for row in manifest['synthetic_inventory']}
        context = dict(schema_version=1, enabled=True, framework_id='odoo', policy_id='odoo-hooks-finite-v1',
            consumer=dict(repository_id='synthetic',revision='0' * 64,source_root_id='synthetic',consumer_id='application',service_id='source'),
            dependency=dict(repository_id='synthetic',revision='0' * 64,source_root_id='synthetic',module_prefix='odoo',identity_kind='synthetic_fixture'),
            source_roots=[dict(id='synthetic',source_prefix='',ownership='one_explicit_admitted_snapshot')],
            ownership=[dict(path=path,consumer_id='application',service_id='source',configuration_namespace='addon') for path in blobs if not path.startswith('odoo/') and path != 'foreign_models.py'] +
                      [dict(path='foreign_models.py',consumer_id='foreign',service_id='foreign',configuration_namespace='other')],
            configurations=[dict(path='data/jobs.xml',manifest_path='__manifest__.py')])
        return manifest, blobs, context

    def test_odoo_collected_declarations_are_finite_and_dispatch_is_unresolved(self):
        manifest, blobs, context = self.odoo_inputs()
        files = [native.collect_file(dict(path=path,language='python',content=raw)) for path,raw in blobs.items() if path.endswith('.py')]
        ordinary = native.resolve_collected(files)['facts']
        result = native.resolve_collected(files,framework_context=context)
        self.assertEqual(result['status'],'complete',result)
        facts = result['facts']; definitions = {row['id']:row for row in facts['definitions']}
        self.assertEqual([row for row in facts['sites'] if not row['role'].startswith('framework')],ordinary['sites'])
        for case in manifest['cases']:
            if case['source_kind'] != 'synthetic' or case['origin']['path'].endswith('.xml'): continue
            origin, expected = case['origin'], case['expected']
            rows = [row for row in facts['sites'] if row['role'].startswith('framework') and row['path'] == origin['path'] and
                    all(row['range'][k] == origin['range'][k] for k in ('start_byte','end_byte'))]
            with self.subTest(case=case['id']):
                self.assertEqual(len(rows),0 if expected['row_family'] is None else 1)
                if not rows: continue
                row = rows[0]
                self.assertEqual((row['family'],row['relation_kind'],len(row['targets'])),(expected['row_family'],expected['row_kind'],expected['target_cardinality']))
                self.assertEqual(row['targets_exhaustive'],expected['targets_exhaustive_for_declared_source_scope'])
                self.assertEqual((row['runtime_dispatch'],row['runtime_callable_targets']),('unresolved',[]))
                for witness in case['witnesses']:
                    self.assertTrue(any(item['path'] == witness['path'] and all(item['range'][k] == witness['range'][k] for k in ('start_byte','end_byte')) for item in row['evidence']),witness)
                for identifier, target in zip(row['targets'],expected['targets']):
                    declaration = definitions[identifier]
                    self.assertEqual(declaration['kind'],'method')
                    self.assertEqual(declaration['name'],target['name'])
                    self.assertEqual({k:declaration['range'][k] for k in ('start_byte','end_byte')},{k:target['range'][k] for k in ('start_byte','end_byte')})
        self.assertFalse(any(row['path'] == 'foreign_models.py' and row['role'].startswith('framework') for row in facts['sites']))
        for path in ('controllers.py','models.py','__manifest__.py'):
            file = next(file for file in files if file.path == path)
            payload = file.to_json(); decoded = native.CollectedFile.from_json(payload,file.record,hashlib.sha256(payload).hexdigest())
            self.assertEqual(decoded.to_json(),payload)
            self.assertEqual(decoded.collected_fact_count,file.collected_fact_count)
            data = json.loads(payload)
            if path == '__manifest__.py': del data['syntax_metadata']['python_dictionaries'][0]['literal']
            elif path == 'models.py': del data['syntax_metadata']['python_declarations'][0]['decorators']
            else: del data['syntax_metadata']['calls'][0]['receiver_root']
            mutated = json.dumps(data).encode()
            with self.assertRaises(ValueError): native.CollectedFile.from_json(mutated,file.record,hashlib.sha256(mutated).hexdigest())

    def test_odoo_shared_configuration_sources_and_incremental_membership(self):
        from repo_graph.analysis import StructuralIndex
        from repo_graph.analysis_queries import Queries
        from repo_graph import search
        from repo_graph.source import SourceRoot
        manifest, blobs, context = self.odoo_inputs()
        with tempfile.TemporaryDirectory() as scratch:
            source,output = Path(scratch)/'source',Path(scratch)/'index'; source.mkdir()
            for path,raw in blobs.items():
                target = source/path; target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            index = StructuralIndex(source,output,framework_context=context)
            ready = index.refresh(sorted(blobs)); self.assertEqual(ready['status'],'ready',ready)
            from repo_graph.cli import main
            buffer=io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(['query',str(output),'--operation','framework','--family','framework','--relation-kind','odoo_route_annotation']),0)
            self.assertEqual(len(json.loads(buffer.getvalue())['rows']),1)
            sites = [row for row in index.read_facts('sites') if row['role'].startswith('framework')]
            definitions = {row['id']:row for row in index.read_facts('definitions')}
            for case in manifest['cases']:
                if case['source_kind'] != 'synthetic' or not case['origin']['path'].endswith('.xml'): continue
                origin,expected = case['origin'],case['expected']
                rows = [row for row in sites if row['path'] == origin['path'] and all(row['range'][k] == origin['range'][k] for k in ('start_byte','end_byte'))]
                with self.subTest(case=case['id']):
                    self.assertEqual(len(rows),1)
                    row = rows[0]
                    self.assertEqual((row['family'],row['relation_kind'],len(row['targets'])),(expected['row_family'],expected['row_kind'],expected['target_cardinality']))
                    for target in row['targets']:
                        self.assertEqual((definitions[target]['kind'],definitions[target]['callable']),('configuration_value',False))
            with Queries(output) as queries:
                result = queries.run(dict(operation='framework',limits=dict(max_edges=100,max_entities=100,max_response_bytes=131072)))
                self.assertFalse(result['truncated'],result)
                engine = search.Search(output)
                try:
                    handles = [row['site'] for row in result['rows']] + [row['target'] for row in result['rows'] if row['target']]
                    handles += [item for row in result['rows'] for item in row['evidence'] if 'id' in item]
                    with patch.object(SourceRoot,'read',side_effect=AssertionError('Query reread source')),patch.object(native,'backend',side_effect=AssertionError('Query parsed source')):
                        for handle in handles:
                            handle = {k:handle[k] for k in ('id','path','range','source_sha256')}
                            inspected = search.captured_source(engine,dict(generation=ready['generation'],handle=handle,max_excerpt_bytes=8192))
                            self.assertTrue(inspected['text'])
                finally: engine.close()
            before_clean = list(index.read_facts('sites'))
            clean = index.refresh(sorted(blobs)); self.assertEqual(clean['status'],'ready',clean)
            self.assertEqual(list(index.read_facts('sites')),before_clean)
            rebuilt = StructuralIndex(source,Path(scratch)/'rebuilt',framework_context=context)
            self.assertEqual(rebuilt.refresh(sorted(blobs),mode='queued',concurrency=2)['status'],'ready')
            self.assertEqual(list(rebuilt.read_facts('sites')),before_clean)
            (source/'__manifest__.py').write_bytes(b'{"data": []}\n')
            removed = index.refresh(sorted(blobs)); self.assertEqual(removed['status'],'ready',removed)
            self.assertFalse(any(row['path'].endswith('.xml') and row['targets'] for row in index.read_facts('sites')))
            (source/'__manifest__.py').write_bytes(blobs['__manifest__.py'])
            self.assertEqual(index.refresh(sorted(blobs))['status'],'ready')
            self.assertTrue(any(row['path'].endswith('.xml') and row['targets'] for row in index.read_facts('sites')))
            for label,changed in {
                'literal_forcecreate':blobs['data/jobs.xml'].replace(b'<record id="invoice_job"',b'<record forcecreate="True" id="invoice_job"'),
                'child_code':blobs['data/jobs.xml'].replace(b'model.action_post()',b'<value>model.action_post()</value>'),
                'cdata_code':blobs['data/jobs.xml'].replace(b'model.action_post()',b'<![CDATA[model.action_post()]]>'),
                'ambiguous_record_child':blobs['data/jobs.xml'].replace(b'<field name="code">model.action_post()</field>',b'<field name="code">model.action_post()</field><other/>'),
                'cross_model_duplicate':blobs['data/jobs.xml'].replace(b'</odoo>',b'<record id="invoice_job" model="unrelated.model"/></odoo>'),
            }.items():
                (source/'data/jobs.xml').write_bytes(changed)
                with self.subTest(configuration=label):
                    self.assertEqual(index.refresh(sorted(blobs))['status'],'ready')
                    targets = [row for row in index.read_facts('sites') if row['path'].endswith('.xml') and row['targets']]
                    self.assertEqual(len(targets),1 if label=='literal_forcecreate' else 0)
            (source/'data/jobs.xml').write_bytes(blobs['data/jobs.xml'])
            other_paths = {'other/__manifest__.py':blobs['__manifest__.py'],'other/data/jobs.xml':blobs['data/jobs.xml']}
            for path,raw in other_paths.items():
                target=source/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            separate_context=copy.deepcopy(context)
            separate_context['ownership'].append(dict(path='other/',consumer_id='application',service_id='source',configuration_namespace='other-addon'))
            separate_context['configurations'].append(dict(path='other/data/jobs.xml',manifest_path='other/__manifest__.py'))
            separate_index=StructuralIndex(source,output,framework_context=separate_context)
            self.assertEqual(separate_index.refresh(sorted([*blobs,*other_paths]))['status'],'ready')
            self.assertEqual(len([row for row in separate_index.read_facts('sites') if row['path'].endswith('.xml') and row['targets']]),2)
            plain=StructuralIndex(source,output)
            self.assertEqual(plain.refresh(sorted(blobs))['status'],'ready')
            self.assertFalse(any(row['role'].startswith('framework') for row in plain.read_facts('sites')))
            duplicate = 'data/duplicate.xml'; (source/duplicate).write_bytes(blobs['data/jobs.xml'].replace(b'model="ir.cron"',b'model="unrelated.model"'))
            duplicate_context = copy.deepcopy(context)
            duplicate_context['ownership'].append(dict(path=duplicate,consumer_id='application',service_id='source',configuration_namespace='addon'))
            duplicate_context['configurations'].append(dict(path=duplicate,manifest_path='__manifest__.py'))
            duplicate_index = StructuralIndex(source,output,framework_context=duplicate_context)
            repeated = duplicate_index.refresh(sorted([*blobs,duplicate])); self.assertEqual(repeated['status'],'ready',repeated)
            self.assertFalse(any(row['path'].endswith('.xml') and row['targets'] for row in duplicate_index.read_facts('sites')))
            self.assertEqual(index.refresh(sorted(blobs))['status'],'ready')
            self.assertTrue(any(row['path'].endswith('.xml') and row['targets'] for row in index.read_facts('sites')))

    def test_odoo_configuration_and_api_negative_guards_are_bounded(self):
        _,blobs,context = self.odoo_inputs()
        for label, changes in {
            'competing_route_module':{'odoo/http/__init__.py':b'def route(*args): pass\n'},
            'missing_model_facade':{'odoo/models/__init__.py':b'from ..orm.models import MissingModel as Model\n'},
            'partial_model_api':{'odoo/orm/models.py':b'class Model(\n'},
            'namespace_member_write':{'models.py':blobs['models.py'] + b'\nmodels.Model = object\n'},
        }.items():
            with self.subTest(guard=label):
                files = [native.collect_file(dict(path=path,language='python',content=raw)) for path,raw in (blobs|changes).items() if path.endswith('.py')]
                result = native.resolve_collected(files,framework_context=context);self.assertIn(result['status'],('complete','partial'),result['stop_reason'])
                kind = 'odoo_route_annotation' if label == 'competing_route_module' else 'odoo_model_method_declaration'
                self.assertFalse(any(row['role']=='framework' and row['relation_kind']==kind for row in result['facts']['sites']))
        changed = blobs['models.py'].replace(b'return "validated"',b'return draft_picking.action_confirm()')
        files = [native.collect_file(dict(path=path,language='python',content=raw)) for path,raw in (blobs|{'models.py':changed}).items() if path.endswith('.py')]
        facts = native.resolve_collected(files,framework_context=context)['facts']
        dispatch = [row for row in facts['sites'] if row['role']=='framework_boundary' and row['text']=='draft_picking.action_confirm()']
        self.assertEqual(len(dispatch),1);self.assertEqual(dispatch[0]['targets'],[])
        for removed,expected in (({'odoo/__init__.py'},5),({'odoo/orm/__init__.py'},0)):
            with self.subTest(missing_namespace=sorted(removed)):
                files = [native.collect_file(dict(path=path,language='python',content=raw)) for path,raw in blobs.items() if path.endswith('.py') and path not in removed]
                result = native.resolve_collected(files,framework_context=context)
                self.assertEqual(result['status'],'complete',result['stop_reason'])
                rows = [row for row in result['facts']['sites'] if row['path']=='models.py' and row.get('candidate_relation_kind')=='odoo_model_method_declaration']
                self.assertEqual(sum(row['family']=='framework' for row in rows),expected)
        raw = blobs['data/jobs.xml']
        for label,changed in {
            'dtd':b'<!DOCTYPE odoo [<!ENTITY x "call">]>'+raw,
            'entity':raw.replace(b'model.action_post()',b'model.action_post(&amp;)'),
            'malformed':raw[:-9],
        }.items():
            with self.subTest(xml=label):
                record = dict(path='data/jobs.xml',sha256=hashlib.sha256(changed).hexdigest(),bytes=len(changed),kind='configuration',language='configuration')
                ir = native.odoo_configuration(record,changed,native.Work(native.Budget(),None))
                self.assertTrue(ir['partial']);self.assertEqual(ir['records'],[])
        record = dict(path='data/jobs.xml',sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),kind='configuration',language='configuration')
        ir = native.odoo_configuration(record,raw,native.Work(native.Budget(),None))
        data = native._json_bytes(ir,native.Budget(),None)
        decoded = native.decode_configuration(data,record,hashlib.sha256(data).hexdigest(),raw,native.Work(native.Budget(),None))
        self.assertEqual(decoded,ir)
        for change in ('missing_metadata','source_text','collector'):
            invalid = copy.deepcopy(ir)
            if change == 'missing_metadata': del invalid['records'][0]['ambiguous']
            elif change == 'source_text': invalid['records'][0]['content']['text'] = 'changed'
            else: invalid['collector_sha256'] = '0' * 64
            data = native._json_bytes(invalid,native.Budget(),None)
            with self.subTest(handoff=change),self.assertRaises(ValueError):
                native.decode_configuration(data,record,hashlib.sha256(data).hexdigest(),raw,native.Work(native.Budget(),None))
        for budget,cancel,reason in ((native.Budget(max_nodes=1),None,'node_budget_exceeded'),(native.Budget(),lambda:True,'cancelled')):
            with self.subTest(reason=reason),self.assertRaises(native.StopScan) as stopped:
                native.odoo_configuration(record,raw,native.Work(budget,cancel))
            self.assertEqual(str(stopped.exception),reason)

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
            'dictionary_escape': {'urls.py': route + b'aliases = {"routes": urlpatterns}\naliases["routes"].clear()\n'},
            'nested_list_escape': {'urls.py': route + b'aliases = [[urlpatterns]]\naliases[0][0].clear()\n'},
            'nested_tuple_escape': {'urls.py': route + b'aliases = ((urlpatterns,),)\naliases[0][0].clear()\n'},
            'implicit_tuple_escape': {'urls.py': route + b'aliases = 0, urlpatterns\naliases[1].clear()\n'},
            'mixed_container_escape': {'urls.py': route + b'aliases = {"routes": ([{"inner": ((urlpatterns))}],)}\naliases["routes"][0][0]["inner"].clear()\n'},
            'conditional_container_escape': {'urls.py': route + b'aliases = {"routes": urlpatterns if enabled else []}\naliases["routes"].clear()\n'},
            'argument_escape': {'urls.py': route + b'mutate(urlpatterns)\n'},
            'nested_argument_escape': {'urls.py': route + b'mutate({"routes": ([urlpatterns],)})\n'},
            'nested_keyword_escape': {'urls.py': route + b'mutate(routes={"inner": [[urlpatterns]]})\n'},
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
                self.assertFalse(rows[0]['targets_exhaustive'])
                self.assertEqual([row for row in facts['sites'] if not row['role'].startswith('framework')], ordinary['sites'])
        # The reviewed finite export exception permits earlier wildcard imports,
        # followed by the final explicit Manager import; it does not resolve '*'.
        files = [native.collect_file(dict(path=path, language='python', content=raw)) for path, raw in original.items()]
        facts = native.resolve_collected(files, framework_context=manifest['source_admission']['frozen_contexts']['synthetic'])['facts']
        self.assertTrue(any(row['role'] == 'framework' and row['relation_kind'] == 'django_orm_get_queryset' for row in facts['sites']))

    def test_nested_container_metadata_preserves_direct_route_elements(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'evaluations/code-understanding/django-framework-inputs.json').read_text())
        original = {record['path']: (root / manifest['fixture_root'] / record['path']).read_bytes()
                    for record in manifest['synthetic_inventory'] if record['path'].endswith('.py')}
        prefix = b'from django.urls import path\n'
        controls = {
            'local_callback': prefix + b'def home(): pass\ncallback = home\nurlpatterns = [path("x/", callback)]\n'
                b'aliases = {"urlpatterns": (["urlpatterns", []],)}\n',
            'unrelated_member': prefix + b'from .views import homepage\nurlpatterns = [path("x/", homepage)]\n'
                b'aliases = {"routes": config.urlpatterns, "settings": configure(urlpatterns=False)}\n',
            'direct_route_with_nested_call': prefix + b'from .views import homepage\n'
                b'urlpatterns = [path("x/", homepage), [path("nested/", homepage)]]\n',
            'nested_only_route': prefix + b'from .views import homepage\nurlpatterns = [[path("nested/", homepage)]]\n',
        }
        for label, raw in controls.items():
            with self.subTest(control=label):
                files = [native.collect_file(dict(path=path, language='python', content=blob))
                         for path, blob in (original | {'urls.py': raw}).items()]
                ordinary = native.resolve_collected(files)['facts']
                result = native.resolve_collected(files, framework_context=manifest['source_admission']['frozen_contexts']['synthetic'])
                self.assertEqual(result['status'], 'complete')
                rows = [row for row in result['facts']['sites'] if row['path'] == 'urls.py' and row['role'].startswith('framework')]
                self.assertEqual(len(rows), 0 if label == 'nested_only_route' else 1, rows)
                for row in rows:
                    self.assertEqual((row['family'], row['certainty'], len(row['targets']), row['targets_exhaustive']),
                                     ('framework', 'resolved', 1, True))
                    self.assertNotIn('nested/', row['text'])
                self.assertEqual([row for row in result['facts']['sites'] if not row['role'].startswith('framework')], ordinary['sites'])
        raw = prefix + b'aliases = {"routes": ([urlpatterns],)}\nmutate(routes={"inner": [[urlpatterns]]})\n'
        file = native.collect_file(dict(path='urls.py', language='python', content=raw))
        assignment = file.syntax_metadata['python_assignments'][0]
        argument = file.syntax_metadata['calls'][0]['arguments'][0]
        for owner in (assignment, argument):
            self.assertEqual([value['spelling'] for value in owner['container_references']], ['urlpatterns'])
        encoded = file.to_json()
        decoded = native.CollectedFile.from_json(encoded, file.record, hashlib.sha256(encoded).hexdigest())
        self.assertEqual(decoded.to_json(), encoded)
        self.assertEqual(decoded.collected_fact_count, file.collected_fact_count)
        for owner_kind in ('assignment', 'argument'):
            for mutation in ('missing', 'foreign_range', 'not_identifier'):
                with self.subTest(owner=owner_kind, mutation=mutation):
                    payload = copy.deepcopy(file.payload())
                    owner = (payload['syntax_metadata']['python_assignments'][0] if owner_kind == 'assignment'
                             else payload['syntax_metadata']['calls'][0]['arguments'][0])
                    if mutation == 'missing': del owner['container_references']
                    elif mutation == 'foreign_range': owner['container_references'][0]['start_byte'] = 0
                    else: owner['container_references'][0]['type'] = 'string'
                    encoded = json.dumps(payload, separators=(',', ':')).encode()
                    with self.assertRaises(ValueError):
                        native.CollectedFile.from_json(encoded, file.record, hashlib.sha256(encoded).hexdigest())

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
