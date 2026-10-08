"""Shared syntax handoff checks; framework resolution is qualified separately."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

from repo_graph import analysis_native as native


AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))


@unittest.skipUnless(AVAILABLE, 'Optional analysis backend')
class FrameworkSyntaxTests(unittest.TestCase):
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
