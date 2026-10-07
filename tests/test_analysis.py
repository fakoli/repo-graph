"""Synthetic source/ownership regressions for the optional structural index."""
import hashlib
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from repo_graph.analysis import IndexLimits, StructuralIndex
from repo_graph import analysis_native as native
from repo_graph import search, source as source_module
from repo_graph.source import SourceRoot


AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))
FACT_KINDS = ('definitions', 'sites', 'imports', 'scopes', 'relationships')


def canonical(rows):
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def facts(index):
    return {kind: canonical(index.read_facts(kind)) for kind in FACT_KINDS}


def write_sources(root, sources):
    for path, content in sources.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode('utf-8'))


def blobs(root, paths):
    languages = {'.py': 'python', '.go': 'go', '.js': 'javascript', '.ts': 'typescript'}
    return [{'path': path, 'language': languages[Path(path).suffix],
             'content': (root / path).read_bytes()} for path in paths]


@unittest.skipUnless(AVAILABLE, 'Optional analysis extra is not installed')
class StructuralIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scratch = Path(self.temporary.name)
        self.root, self.output = self.scratch / 'repo', self.scratch / 'out'
        self.root.mkdir()

    def ready(self, index, paths, **options):
        result = index.refresh(paths, **options)
        self.assertEqual(result['status'], 'ready', result)
        self.assertTrue(result['generation'])
        self.assertTrue(result['source_identity'])
        return result

    def clean_parity(self, index, paths):
        clean = native.extract(blobs(self.root, paths))
        self.assertEqual(clean['status'], 'complete', clean)
        for kind in ('definitions', 'sites'):
            self.assertEqual(canonical(index.read_facts(kind)), canonical(clean['facts'][kind]))

    def test_function_projection_uses_captured_text_and_rolls_back_with_structural_stage(self):
        from repo_graph import analysis as index_module
        write_sources(self.root, {'main.py': 'def oldFunction(): pass\n'})
        index = StructuralIndex(self.root, self.output)
        original_read, projector = SourceRoot.read, search.project_function_evidence
        reads = []
        def observed_read(boundary, *args, **kwargs):
            if boundary.root == self.root: reads.append(args[0])
            return original_read(boundary, *args, **kwargs)
        def captured_only(db, identity, *, check, limits=None):
            before = list(reads)
            forbidden = AssertionError('Projection must consume captured facts/text')
            with patch.object(native, 'collect_file', side_effect=forbidden), \
                    patch.object(native, 'extract', side_effect=forbidden):
                result = projector(db, identity, check=check, limits=limits)
            self.assertEqual(reads, before)
            return result
        with patch.object(SourceRoot, 'read', observed_read), \
                patch.object(index_module, 'project_function_evidence', captured_only):
            previous = self.ready(index, ['main.py'])
        before = (self.output / 'search.db').read_bytes()
        old_facts = facts(index)
        write_sources(self.root, {'main.py': 'def newFunction(): pass\n'})
        def exhausted(db, identity, *, check, limits=None):
            return projector(db, identity, check=check,
                             limits=search.FunctionProjectionLimits(max_body_bytes=1))
        with patch.object(index_module, 'project_function_evidence', exhausted):
            failed = index.refresh(['main.py'])
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(failed['published'])
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata()['generation'], previous['generation'])
        self.assertEqual(facts(reopened), old_facts)
        ready = self.ready(reopened, ['main.py'])
        self.assertNotEqual(ready['generation'], previous['generation'])
        result = search.Search(self.output).run('newFunction', kind='functions', mode='keyword')
        self.assertEqual(result['identities']['structural_generation'], ready['generation'])
        self.assertTrue(any(member['name'] == 'newFunction' for row in result['results'] for member in row['members']))
        self.assertFalse(any(member['name'] == 'oldFunction' for row in result['results'] for member in row['members']))

    def test_captured_partial_coverage_and_failed_attempt_survive_reopen(self):
        sources = {'main.py': 'def target(): return 1\n',
            'partial.py': 'def local():\n    return 1\ndef caller():\n    return local()\n! broken [\n',
            'other.rs': 'fn undiscovered() {}\n'}
        write_sources(self.root, sources)
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            with self.subTest(mode=mode):
                index = StructuralIndex(self.root, self.scratch / ('coverage-' + mode))
                previous = self.ready(index, ['main.py', 'partial.py',
                    {'path': 'other.rs', 'language': 'rust'}], mode=mode, concurrency=concurrency)
                status = search.index_status(index.output, owner=index.output_owner)
                published = status['structural']
                self.assertTrue(published['artifact_ready'])
                self.assertEqual(published['freshness'], 'unknown')
                coverage = published['receipt']['coverage']
                self.assertEqual(coverage['file_status'], {'parsed': 1, 'partial_parse': 1, 'unsupported_language': 1})
                self.assertEqual(coverage['files_total'], 3)
                self.assertEqual(coverage['by_language']['rust'], {'files_total': 1, 'file_status': {'unsupported_language': 1}})
                self.assertGreater(coverage['parser_error_count'], 0)
                self.assertFalse(coverage['parser_error_samples_truncated'])
                for sample in coverage['parser_error_samples']:
                    self.assertEqual(sample['path'], 'partial.py')
                    self.assertLessEqual(sample['range']['end_byte'], len(sources['partial.py'].encode()))
                calls = [site for site in index.read_facts('sites') if site['text'] == 'local()']
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]['certainty'], 'unresolved')
                self.assertFalse(calls[0]['targets_exhaustive'])
                self.assertEqual(calls[0]['targets'], [])
                self.assertTrue(calls[0]['reason'])
                retained, artifact = facts(index), (index.output / 'search.db').read_bytes()
                consumed = []
                def entries():
                    consumed.append('missing.py'); yield 'missing.py'
                    consumed.append('unread.py'); yield 'unread.py'
                failed = index.refresh(entries(), mode=mode, concurrency=concurrency)
                self.assertEqual(failed['status'], 'failed')
                self.assertFalse(failed['published'])
                self.assertEqual(consumed, ['missing.py'])
                self.assertEqual(failed['path'], 'missing.py')
                self.assertEqual(failed['remaining_inventory_status'], 'not_evaluated_after_failure')
                self.assertEqual(failed['resources']['inventory_entries_consumed'], 1)
                self.assertEqual(failed['published_coverage_generation'], previous['generation'])
                self.assertEqual((index.output / 'search.db').read_bytes(), artifact)
                reopened = StructuralIndex(self.root, index.output)
                captured = search.index_status(reopened.output, owner=reopened.output_owner)['structural']
                self.assertEqual(captured['state'], 'failed')
                self.assertTrue(captured['artifact_ready'])
                self.assertEqual(captured['last_attempt']['status'], 'failed')
                self.assertEqual(captured['identities']['generation'], previous['generation'])
                self.assertEqual(captured['receipt'], published['receipt'])
                self.assertEqual(facts(reopened), retained)

    def test_git_revision_capture_keeps_dirty_unknown_and_avoids_configured_filters(self):
        sources = {'main.py': 'def target(): return 1\n', '.gitattributes': 'main.py filter=marker\n',
            'marker-filter.sh': "printf 'ran\\n' >> marker.txt\ncat\n"}
        write_sources(self.root, sources)
        git = ['git', '-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgSign=false', '-c', 'core.hooksPath=/dev/null', '-c', 'init.templateDir=']
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_COUNT='0',
            GIT_AUTHOR_NAME='Synthetic Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
            GIT_COMMITTER_NAME='Synthetic Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid',
            GIT_AUTHOR_DATE='2000-01-01T00:00:00+0000', GIT_COMMITTER_DATE='2000-01-01T00:00:00+0000',
            GIT_TERMINAL_PROMPT='0')
        def command(args):
            return subprocess.run(git + args, cwd=self.root, env=env, check=True, capture_output=True, timeout=5)
        for args in (['init', '-q'], ['add', '--', *sources], ['commit', '-q', '-m', 'Synthetic filter fixture']):
            command(args)
        index = StructuralIndex(self.root, self.output)
        initial = self.ready(index, ['main.py'])
        self.assertEqual(initial['revision_dirty']['knowledge'], 'captured_revision')
        self.assertIsNone(initial['revision_dirty']['dirty'])
        frozen_revision = initial['revision_dirty']['revision']
        self.assertTrue(frozen_revision)
        marker = self.root / 'marker.txt'
        self.assertFalse(marker.exists())
        conditional_include = 'includeIf.gitdir:' + str(self.root / '.git') + '.path'
        for number, configuration in enumerate(('filter', 'include', 'includeIf', 'worktree'), 2):
            with self.subTest(configuration=configuration):
                if configuration == 'filter':
                    command(['config', '--local', 'filter.marker.clean', 'sh marker-filter.sh'])
                elif configuration == 'include':
                    command(['config', '--local', '--unset-all', 'filter.marker.clean'])
                    included = self.root / '.git' / 'included-config'
                    included.write_text('[filter "marker"]\n\tclean = sh marker-filter.sh\n')
                    command(['config', '--local', 'include.path', 'included-config'])
                elif configuration == 'includeIf':
                    command(['config', '--local', '--unset-all', 'include.path'])
                    command(['config', '--local', conditional_include, 'included-config'])
                else:
                    command(['config', '--local', '--unset-all', conditional_include])
                    command(['config', '--local', 'extensions.worktreeConfig', 'true'])
                    (self.root / '.git' / 'config.worktree').write_text('[filter "marker"]\n\tclean = sh marker-filter.sh\n')
                before = (self.root / 'main.py').stat()
                edited = 'def target(): return %d\n' % number
                self.assertEqual(len(edited.encode()), len(sources['main.py'].encode()))
                write_sources(self.root, {'main.py': edited})
                os.utime(self.root / 'main.py', ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                receipt = self.ready(index, ['main.py'])
                self.assertFalse(marker.exists(), 'Git source capture executed a repository clean filter')
                captured = receipt['revision_dirty']
                self.assertEqual(captured['knowledge'], 'captured_revision')
                self.assertEqual(captured['revision'], frozen_revision)
                self.assertIsNone(captured['dirty'])
                self.assertEqual(captured['reason'], 'git_dirty_not_observed_without_project_commands')
                self.assertEqual(captured['dirty_basis'], 'unobserved_repository_configured_status')
                self.assertEqual(captured['content_identity'], receipt['source_identity'])
                self.assertNotEqual(receipt['source_identity'], initial['source_identity'])

    def test_persisted_facts_round_trip_physical_utf8_and_lexical_links(self):
        source = '# élève 東京\r\ndef café():\r\n    return 1\r\ndef appel():\r\n    return café()\r\n'
        write_sources(self.root, {'main.py': source})
        index = StructuralIndex(self.root, self.output)
        raw = source.encode('utf-8')
        inventory = [{'path': 'main.py', 'language': 'python', 'kind': 'source',
                      'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}]
        result = self.ready(index, inventory)
        self.clean_parity(index, ['main.py'])
        retained = facts(index)
        for row in retained['definitions'] + retained['sites']:
            location = row['range']
            self.assertEqual(raw[location['start_byte']:location['end_byte']].decode('utf-8'), row['text'])
            self.assertEqual(row['provenance']['source_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row['provenance']['evidence_kind'], 'static_syntax')
        call = next(row for row in retained['sites'] if row['role'] == 'call')
        target = next(row for row in retained['definitions'] if row['name'] == 'café')
        self.assertEqual(call['targets'], [target['id']])
        self.assertTrue(any(row['path'] == 'main.py' and row['kind'] == 'module' for row in retained['scopes']))
        self.assertTrue(any(row['path'] == 'main.py' and row['name'] == 'appel' and row['parent'] is not None
                            for row in retained['scopes']))
        self.assertTrue(any(row['site_id'] == call['id'] and row['target_id'] == target['id']
                            for row in retained['relationships']))
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(facts(reopened), retained)
        metadata = reopened.metadata()
        self.assertEqual(metadata['generation'], result['generation'])
        for key in ('repository_identity', 'analyzer_identity', 'config_identity'):
            self.assertTrue(metadata[key])
        self.assertEqual([path.name for path in self.output.glob('*.db')], ['search.db'])
        with self.assertRaises(ValueError):
            list(reopened.read_facts('invented'))

    def test_serial_and_queued_modes_publish_identical_source_facts(self):
        sources = {
            'main.py': 'def target(): return 1\ndef call(): return target()\n',
            'main.go': 'package main\nfunc Target() int { return 1 }\nfunc Call() int { return Target() }\n',
            'main.js': 'function target() { return 1; }\nfunction call() { return target(); }\n',
            'main.ts': 'function target(): number { return 1; }\nfunction call(): number { return target(); }\n',
        }
        write_sources(self.root, sources)
        serial = StructuralIndex(self.root, self.output, limits=IndexLimits(batch_files=2))
        queued = StructuralIndex(self.root, self.scratch / 'queued', limits=IndexLimits(batch_files=2))
        serial_result = self.ready(serial, iter(sources), mode='serial', concurrency=1)
        queued_result = self.ready(queued, iter(sources), mode='queued', concurrency=2)
        self.assertEqual(facts(serial), facts(queued))
        self.assertEqual(serial_result['source_identity'], queued_result['source_identity'])
        self.assertEqual(serial_result['coverage'], queued_result['coverage'])
        self.assertEqual(serial_result['resources']['batches'], 2)
        self.assertEqual(queued_result['resources']['batches'], 2)
        self.clean_parity(queued, list(sources))

    def test_lazy_single_file_batches_resolve_imports_after_all_collection(self):
        sources = {'package/caller.py': 'from .provider import target as alias\ndef call(): return alias()\n',
                   'package/provider.py': 'def target(): return 1\n',
                   'package/absolute.py': 'from package.provider import target as alias\ndef call(): return alias()\n'}
        write_sources(self.root, sources)
        index = StructuralIndex(self.root, self.output, limits=IndexLimits(batch_files=1))
        visited = []
        def inventory():
            for path in sources:
                visited.append(path)
                yield path
        result = self.ready(index, inventory())
        self.assertEqual(visited, list(sources))
        self.assertEqual(result['resources']['batches'], 3)
        self.clean_parity(index, list(sources))
        absolute = next(row for row in index.read_facts('sites')
                        if row['role'] == 'call' and row['path'] == 'package/absolute.py')
        self.assertEqual((absolute['certainty'], absolute['targets']), ('unresolved', []))
        self.assertIn('absolute Python import environment', absolute['reason'])
        call = next(row for row in index.read_facts('sites')
                    if row['role'] == 'call' and row['path'] == 'package/caller.py')
        self.assertEqual(call['certainty'], 'resolved')
        self.assertEqual(len(call['targets']), 1)
        sources['package/provider.py'] = 'def renamed(): return 1\n'
        write_sources(self.root, {'package/provider.py': sources['package/provider.py']})
        updated = self.ready(index, (path for path in sources))
        self.assertEqual(updated['resources']['changed_files_collected'], 1)
        self.assertEqual(updated['resources']['unchanged_source_collections_reused'], 2)
        self.clean_parity(index, list(sources))
        call = next(row for row in index.read_facts('sites')
                    if row['role'] == 'call' and row['path'] == 'package/caller.py')
        self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
        self.assertEqual(list(index.read_facts('relationships')), [])

    def test_reopen_reuses_unchanged_collections_without_reparsing(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\ndef call(): return target()\n'})
        original = StructuralIndex(self.root, self.output)
        first = self.ready(original, ['main.py'])
        retained = facts(original)
        reopened = StructuralIndex(self.root, self.output)
        with patch.object(native, 'collect_file', side_effect=AssertionError('Unchanged source reparsed')), \
                patch.object(native.CollectedFile, 'emit_sites', side_effect=AssertionError('Unchanged bindings rerun')):
            second = self.ready(reopened, ['main.py'])
        self.assertEqual(second['resources']['changed_files_collected'], 0)
        self.assertEqual(second['resources']['unchanged_source_collections_reused'], 1)
        self.assertEqual(second['resources']['bindings_files_resolved'], 0)
        self.assertEqual(second['resources']['bindings_files_reused'], 1)
        self.assertEqual(second['source_identity'], first['source_identity'])
        self.assertEqual(second['generation'], first['generation'])
        self.assertEqual(facts(reopened), retained)

    def test_imports_references_calls_and_unknowns_remain_distinct(self):
        sources = {'package/provider.py': 'def finish(value): return value * 2\n',
            'package/main.py': '# café λ\nfrom .provider import finish as imported\nfrom .missing import added\n'
                'def local(value): return value + 1\n'
                'def reference(): return local\n'
                'def alias():\n    next_step = local\n    return next_step(1)\n'
                'def shadow():\n    def local(value): return value + 100\n    return local(2)\n'
                'def callback(fn): return fn(3)\n'
                'def dynamic(table, name): return table[name](4)\n'
                'def imported_call(): return imported(5)\n'
                'def missing_call(): return added(6)\n'}
        write_sources(self.root, sources)
        index = StructuralIndex(self.root, self.output)
        self.ready(index, iter(sources), mode='queued', concurrency=2)
        retained = facts(index)
        imports = retained['imports']
        self.assertEqual({row['name'] for row in imports}, {'imported', 'added'})
        self.assertTrue(all(row['role'] == 'import' for row in imports))
        self.assertTrue(all(row['role'] in ('call', 'reference') for row in retained['sites']))
        reference = next(row for row in retained['definitions'] if row['name'] == 'reference')
        self.assertTrue(any(row['role'] == 'reference' and row['text'] == 'local' and
                            row['caller'] == reference['id'] for row in retained['sites']))
        by_text = {row['text']: row for row in retained['sites'] if row['role'] == 'call'}
        self.assertEqual(by_text['imported(5)']['certainty'], 'resolved')
        self.assertEqual(by_text['next_step(1)']['certainty'], 'resolved')
        shadow = next(row for row in retained['definitions'] if row['name'] == 'shadow.local')
        self.assertEqual(by_text['local(2)']['targets'], [shadow['id']])
        for text in ('fn(3)', 'table[name](4)', 'added(6)'):
            with self.subTest(text=text):
                self.assertEqual((by_text[text]['certainty'], by_text[text]['targets']), ('unresolved', []))
                self.assertFalse(by_text[text]['targets_exhaustive'])
                self.assertTrue(by_text[text]['reason'])
        for row in imports + retained['definitions'] + retained['sites']:
            raw = sources[row['path']].encode('utf-8')
            location = row['range']
            self.assertEqual(raw[location['start_byte']:location['end_byte']].decode(), row['text'])
            self.assertEqual(row['provenance']['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.clean_parity(index, list(sources))

    def test_incremental_known_bindings_reuse_unrelated_files(self):
        sources = {'first.py': 'def first(): return 1\ndef invoke(): return first()\n',
                   'other.py': 'def other(): return 2\ndef invoke(): return other()\n'}
        write_sources(self.root, sources)
        index = StructuralIndex(self.root, self.output)
        initial = self.ready(index, sources)
        sources['first.py'] = sources['first.py'].replace('return 1', 'return 9')
        write_sources(self.root, sources)
        changed = self.ready(index, sources, mode='queued', concurrency=2)
        self.assertEqual(changed['resources']['changed_files_collected'], 1)
        self.assertEqual(changed['resources']['bindings_files_resolved'], 1)
        self.assertEqual(changed['resources']['bindings_files_reused'], 1)
        self.assertEqual(changed['resources']['unknown_closure_files_rebuilt'], 0)
        self.assertNotEqual(initial['generation'], changed['generation'])
        clean = StructuralIndex(self.root, self.scratch / 'clean-known')
        rebuilt = self.ready(clean, sources)
        self.assertEqual((changed['generation'], changed['source_identity']),
                         (rebuilt['generation'], rebuilt['source_identity']))
        self.assertEqual(facts(index), facts(clean))

    def test_negative_import_add_delete_and_new_cycles_remain_fresh_in_both_modes(self):
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            with self.subTest(mode=mode):
                sources = {'package/caller.py': 'from .optional import target\ndef caller(): return target()\n',
                           'known.py': 'def known(): return 1\n'}
                write_sources(self.root, sources)
                index = StructuralIndex(self.root, self.scratch / ('negative-' + mode))
                self.ready(index, sources, mode=mode, concurrency=concurrency)
                call = lambda: next(row for row in index.read_facts('sites') if row['role'] == 'call' and
                                    row['path'] == 'package/caller.py')
                self.assertEqual(call()['certainty'], 'unresolved')
                sources['package/optional.py'] = 'def target(): return 2\n'
                write_sources(self.root, sources)
                added = self.ready(index, sources, mode=mode, concurrency=concurrency)
                self.assertEqual(added['resources']['changed_files_collected'], 1)
                self.assertEqual(call()['certainty'], 'resolved')
                del sources['package/optional.py']
                (self.root / 'package/optional.py').unlink()
                deleted = self.ready(index, sources, mode=mode, concurrency=concurrency)
                self.assertEqual(deleted['resources']['changed_files_collected'], 0)
                self.assertEqual(call()['certainty'], 'unresolved')
                self.assertFalse(call()['targets_exhaustive'])
                self.assertFalse(any(row['path'] == 'package/optional.py' for row in index.read_facts('definitions')))
                sources.update({'package/left.py': 'from . import right\ndef left(): return right.right()\n',
                                'package/right.py': 'from . import left\ndef right(): return left.left()\n'})
                write_sources(self.root, sources)
                self.ready(index, sources, mode=mode, concurrency=concurrency)
                definitions = {row['id']: row for row in index.read_facts('definitions')}
                for path, target in (('package/left.py', 'package/right.py'), ('package/right.py', 'package/left.py')):
                    site = next(row for row in index.read_facts('sites') if row['path'] == path and row['role'] == 'call')
                    self.assertEqual(site['certainty'], 'resolved')
                    self.assertEqual([definitions[identity]['path'] for identity in site['targets']], [target])
                clean = StructuralIndex(self.root, self.scratch / ('clean-negative-' + mode))
                self.ready(clean, sources)
                self.assertEqual(facts(index), facts(clean))
                sources['package/__init__.py'] = 'right = 1\n'
                write_sources(self.root, sources)
                self.ready(index, sources, mode=mode, concurrency=concurrency)
                left = next(row for row in index.read_facts('sites') if row['path'] == 'package/left.py' and row['role'] == 'call')
                self.assertEqual((left['certainty'], left['targets']), ('unresolved', []))
                self.assertFalse(left['targets_exhaustive'])
                excluded = self.ready(index, [path for path in sources if path != 'package/__init__.py'] +
                    [{'path': 'package/__init__.py', 'language': 'unknown', 'kind': 'source'}],
                    mode=mode, concurrency=concurrency)
                self.assertEqual(excluded['coverage']['files_unsupported'], 1)
                left = next(row for row in index.read_facts('sites') if row['path'] == 'package/left.py' and row['role'] == 'call')
                self.assertEqual((left['certainty'], left['targets']), ('unresolved', []))
                self.assertFalse(left['targets_exhaustive'])
                del sources['package/__init__.py']
                (self.root / 'package/__init__.py').unlink()
                sources['package/right/__init__.py'] = 'def right(): return 3\n'
                write_sources(self.root, sources)
                self.ready(index, sources, mode=mode, concurrency=concurrency)
                left = next(row for row in index.read_facts('sites') if row['path'] == 'package/left.py' and row['role'] == 'call')
                self.assertEqual((left['certainty'], left['targets']), ('unresolved', []))
                self.assertFalse(left['targets_exhaustive'])
                ambiguous = StructuralIndex(self.root, self.scratch / ('clean-ambiguous-' + mode))
                self.ready(ambiguous, sources)
                self.assertEqual(facts(index), facts(ambiguous))
                excluded_inventory = [path for path in sources if path != 'package/right/__init__.py'] + \
                    [{'path': 'package/right/__init__.py', 'language': 'unknown', 'kind': 'source'}]
                excluded_child = self.ready(index, excluded_inventory, mode=mode, concurrency=concurrency)
                self.assertEqual(excluded_child['coverage']['files_unsupported'], 1)
                left = next(row for row in index.read_facts('sites') if row['path'] == 'package/left.py' and row['role'] == 'call')
                self.assertEqual((left['certainty'], left['targets']), ('unresolved', []))
                self.assertFalse(left['targets_exhaustive'])
                excluded_clean = StructuralIndex(self.root, self.scratch / ('clean-excluded-child-' + mode))
                self.ready(excluded_clean, excluded_inventory)
                self.assertEqual(facts(index), facts(excluded_clean))
                sources['right.py'] = 'def right(): return 4\n'
                for statement, invocation in (('from ... import right', 'right.right()'),
                                              ('from ...right import right', 'right()')):
                    sources['package/left.py'] = statement + '\ndef left(): return ' + invocation + '\n'
                    write_sources(self.root, sources)
                    self.ready(index, sources, mode=mode, concurrency=concurrency)
                    left = next(row for row in index.read_facts('sites') if row['path'] == 'package/left.py' and row['role'] == 'call')
                    self.assertEqual((left['certainty'], left['targets']), ('unresolved', []))
                    self.assertFalse(left['targets_exhaustive'])
                    self.assertTrue(left['reason'])
                    escaped = StructuralIndex(self.root, self.scratch / ('clean-ascent-' + mode + str(len(statement))))
                    self.ready(escaped, sources)
                    self.assertEqual(facts(index), facts(escaped))

    def test_configuration_invalidates_go_import_without_recollecting_source(self):
        sources = {'go.mod': 'module sample\n\ngo 1.23\n',
            'main.go': 'package sample\nimport alias "sample/helpers"\nfunc Caller() { alias.Finish() }\n',
            'helpers/helpers.go': 'package helpers\nfunc Finish() {}\n'}
        write_sources(self.root, sources)
        index = StructuralIndex(self.root, self.output)
        initial = self.ready(index, sources)
        call = lambda: next(row for row in index.read_facts('sites') if row['role'] == 'call')
        self.assertEqual(call()['certainty'], 'resolved')
        sources['go.mod'] = sources['go.mod'].replace('module sample', 'module changed')
        write_sources(self.root, sources)
        changed = self.ready(index, sources, mode='queued', concurrency=2)
        self.assertEqual(changed['resources']['changed_files_collected'], 0)
        self.assertEqual(changed['resources']['unchanged_source_collections_reused'], 2)
        self.assertGreaterEqual(changed['resources']['bindings_files_resolved'], 1)
        self.assertEqual(call()['certainty'], 'unresolved')
        self.assertFalse(call()['targets_exhaustive'])
        self.assertNotEqual(initial['generation'], changed['generation'])
        clean = StructuralIndex(self.root, self.scratch / 'clean-config')
        rebuilt = self.ready(clean, sources)
        self.assertEqual((changed['generation'], changed['source_identity']),
                         (rebuilt['generation'], rebuilt['source_identity']))
        self.assertEqual(facts(index), facts(clean))

    def test_unknown_consumer_rebuilds_and_failed_publication_keeps_prior_generation(self):
        sources = {'unknown.py': 'def callback(fn): return fn()\n',
                   'known.py': 'def target(): return 1\ndef invoke(): return target()\n'}
        write_sources(self.root, sources)
        index = StructuralIndex(self.root, self.output)
        self.ready(index, sources)
        sources['known.py'] = sources['known.py'].replace('return 1', 'return 9')
        write_sources(self.root, sources)
        changed = self.ready(index, sources)
        self.assertEqual(changed['resources']['bindings_files_resolved'], 2)
        self.assertEqual(changed['resources']['bindings_files_reused'], 0)
        self.assertEqual(changed['resources']['unknown_closure_files_rebuilt'], 1)
        unknown = next(row for row in index.read_facts('sites') if row['role'] == 'call' and row['path'] == 'unknown.py')
        self.assertEqual(unknown['certainty'], 'unresolved')
        self.assertFalse(unknown['targets_exhaustive'])
        clean = StructuralIndex(self.root, self.scratch / 'clean-unknown')
        self.ready(clean, sources)
        self.assertEqual(facts(index), facts(clean))
        retained, metadata = facts(index), index.metadata()
        before = (self.output / 'search.db').read_bytes()
        sources['known.py'] = sources['known.py'].replace('return 9', 'return 11')
        write_sources(self.root, sources)
        atomic_writer = SourceRoot.atomic_writer
        def fail_before_replace(owner, name, *, text=False, before_replace=None):
            def fail():
                if before_replace is not None:
                    before_replace()
                raise OSError('Synthetic publication failure')
            return atomic_writer(owner, name, text=text,
                before_replace=fail if name == 'search.db' else before_replace)
        with patch.object(SourceRoot, 'atomic_writer', new=fail_before_replace):
            failed = index.refresh(sources)
        self.assertEqual((failed['status'], failed['published']), ('failed', False))
        self.assertEqual(failed['previous_generation'], metadata['generation'])
        self.assertEqual(failed['published_coverage_generation'], metadata['generation'])
        self.assertEqual(failed['remaining_inventory_status'], 'not_evaluated_after_failure')
        self.assertIsInstance(failed['collection_failures'], list)
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata(), metadata)
        self.assertEqual(facts(reopened), retained)

    def test_unsupported_inventory_remains_visible_without_source_facts(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\n',
                                  'other.rs': 'fn undiscovered() {}\n'})
        index = StructuralIndex(self.root, self.output)
        result = self.ready(index, ['main.py', 'other.rs'])
        self.assertEqual(result['coverage']['files_total'], 2)
        self.assertEqual(result['coverage']['files_supported'], 1)
        self.assertEqual(result['coverage']['files_unsupported'], 1)
        for kind in FACT_KINDS:
            self.assertFalse(any(row.get('path') == 'other.rs' for row in index.read_facts(kind)))
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata()['generation'], result['generation'])

    def test_explicit_output_cannot_exchange_facts_between_equal_source_roots(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\n'})
        owned = StructuralIndex(self.root, self.output)
        self.ready(owned, ['main.py'])
        retained, metadata = facts(owned), owned.metadata()
        before = (self.output / 'search.db').read_bytes()
        foreign = self.scratch / 'foreign'
        foreign.mkdir()
        write_sources(foreign, {'main.py': 'def target(): return 1\n'})
        stamp = (self.root / 'main.py').stat()
        os.utime(foreign / 'main.py', ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        try:
            index = StructuralIndex(foreign, self.output)
            result = index.refresh(['main.py'])
        except (ValueError, OSError, RuntimeError):
            pass
        else:
            self.assertIn(result['status'], ('failed', 'interrupted'))
            self.assertFalse(result['published'])
            with self.assertRaises((ValueError, OSError, RuntimeError)):
                list(index.read_facts('definitions'))
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata(), metadata)
        self.assertEqual(facts(reopened), retained)

    def test_shared_output_rejects_foreign_owners_in_both_writer_orders(self):
        write_sources(self.root, {'main.py': 'def owned_target(): return 1\n'})
        foreign = self.scratch / 'foreign'
        foreign.mkdir()
        write_sources(foreign, {'main.py': 'def foreign_target(): return 2\n'})
        for first in ('structural', 'keyword'):
            with self.subTest(first=first):
                output = self.scratch / first
                owned = StructuralIndex(self.root, output)
                if first == 'structural':
                    self.ready(owned, ['main.py'])
                    before = (output / 'search.db').read_bytes()
                    with self.assertRaises(RuntimeError):
                        search.catalog(foreign, ['main.py'], output)
                    self.assertEqual((output / 'search.db').read_bytes(), before)
                    self.assertEqual(search.catalog(self.root, ['main.py'], output)['failed'], 0)
                else:
                    self.assertEqual(search.catalog(self.root, ['main.py'], output)['failed'], 0)
                    before = (output / 'search.db').read_bytes()
                    outsider = StructuralIndex(foreign, output)
                    refused = outsider.refresh(['main.py'])
                    self.assertEqual(refused['status'], 'failed')
                    self.assertFalse(refused['published'])
                    with self.assertRaises(RuntimeError):
                        list(outsider.read_facts('definitions'))
                    self.assertEqual((output / 'search.db').read_bytes(), before)
                    self.ready(owned, ['main.py'])
                self.assertEqual([row['name'] for row in owned.read_facts('definitions')], ['owned_target'])
                self.assertEqual(search.Search(output).run('owned', mode='keyword')['results'][0]['path'], 'main.py')
                self.assertEqual(search.Search(output).run('foreign', mode='keyword')['results'], [])

    def test_post_replace_fsync_failure_reports_actual_published_generation(self):
        write_sources(self.root, {'main.py': 'def before(): return 1\n'})
        index = StructuralIndex(self.root, self.output)
        original = self.ready(index, ['main.py'])
        before = (self.output / 'search.db').read_bytes()
        write_sources(self.root, {'main.py': 'def after(): return 2\n'})
        output = self.output.stat()
        sync = os.fsync
        def fail_output_directory(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (output.st_dev, output.st_ino):
                raise OSError(5, 'synthetic directory synchronization failure')
            return sync(fd)
        with patch.object(source_module.os, 'fsync', fail_output_directory):
            refused_begin = index.refresh(['main.py'])
        self.assertEqual(refused_begin['status'], 'failed')
        self.assertFalse(refused_begin['published'])
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        self.assertEqual(index.metadata()['generation'], original['generation'])
        self.assertEqual([row['name'] for row in index.read_facts('definitions')], ['before'])
        injected = [False]
        def fail_after_database_replace(fd):
            info = os.fstat(fd)
            if (not injected[0] and stat.S_ISDIR(info.st_mode) and
                    (info.st_dev, info.st_ino) == (output.st_dev, output.st_ino) and
                    (self.output / 'search.db').read_bytes() != before):
                injected[0] = True
                raise OSError(5, 'synthetic directory synchronization failure')
            return sync(fd)
        with patch.object(source_module.os, 'fsync', fail_after_database_replace):
            result = index.refresh(['main.py'])
        self.assertTrue(injected[0])
        self.assertEqual(result['status'], 'publication_uncertain')
        self.assertTrue(result['published'])
        self.assertEqual(result['durability'], 'unconfirmed')
        self.assertNotEqual(result['generation'], original['generation'])
        self.assertNotEqual((self.output / 'search.db').read_bytes(), before)
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata()['generation'], result['generation'])
        self.assertEqual([row['name'] for row in reopened.read_facts('definitions')], ['after'])

    def test_loaded_source_and_writer_drift_are_rejected_in_private_copy(self):
        code = self.scratch / 'code'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'repo_graph', code / 'repo_graph',
                        ignore=shutil.ignore_patterns('__pycache__'))
        script = '''import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from repo_graph import analysis, source, search
code = Path(sys.argv[1])
assert all(Path(module.__file__).is_relative_to(code) for module in (analysis, source, search))
root = code / "repo"
root.mkdir()
(root / "main.py").write_text("def target(): return 1\\n")
index = analysis.StructuralIndex(root, code / "out")
first = index.refresh(["main.py"])
assert first["status"] == "ready", first
before = (code / "out/search.db").read_bytes()
outcomes = {}
for name in ("source.py", "search.py"):
    helper = code / "repo_graph" / name
    original = helper.read_bytes()
    try:
        helper.write_bytes(original + b"\\n# synthetic loaded-code drift\\n")
        result = index.refresh(["main.py"])
        assert result["status"] == "failed" and result["published"] is False, result
        assert "implementation changed since module import" in result["reason"], result
        assert (code / "out/search.db").read_bytes() == before
        outcomes[name] = {"status": result["status"], "published": result["published"]}
    finally:
        helper.write_bytes(original)
    assert index.metadata()["generation"] == first["generation"]
print(json.dumps(outcomes))
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(code)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'source.py': {'status': 'failed', 'published': False},
            'search.py': {'status': 'failed', 'published': False}})

    def test_duplicate_go_package_declarations_remain_unresolved(self):
        write_sources(self.root, {'go.mod': 'module sample\n\ngo 1.23\n',
            'main.go': 'package sample\nimport alias "sample/helpers"\n'
                'func Target() {}\nfunc Caller() { Target() }\nfunc Imported() { alias.Finish() }\n',
            'other.go': 'package sample\nfunc Other() {}\n',
            'helpers/helpers.go': 'package helpers\nfunc Finish() {}\n'})
        control = StructuralIndex(self.root, self.output)
        self.ready(control, ['go.mod', 'main.go', 'other.go', 'helpers/helpers.go'])
        unique = next(row for row in control.read_facts('sites') if row['role'] == 'call' and row['text'] == 'Target()')
        self.assertEqual(unique['certainty'], 'resolved')
        self.assertTrue(unique['targets_exhaustive'])
        imported = next(row for row in control.read_facts('sites') if row['role'] == 'call' and row['text'] == 'alias.Finish()')
        self.assertEqual(imported['certainty'], 'resolved')
        self.assertEqual(imported['provenance']['binding_scope'], 'inventoried_package_only')
        self.assertFalse(imported['provenance']['active_build_qualified'])
        self.assertFalse(imported['provenance']['runtime_qualified'])
        self.assertFalse(imported['provenance']['mvs_qualified'])
        for declaration in ('func Target() {}\n', 'var Target = func() {}\n'):
            write_sources(self.root, {'go.mod': 'module sample\n\ngo 1.23\n',
                'main.go': 'package sample\nfunc Target() {}\nfunc Caller() { Target() }\n',
                'other.go': 'package sample\n' + declaration})
            for mode, concurrency in (('serial', 1), ('queued', 2)):
                with self.subTest(declaration=declaration, mode=mode):
                    output = self.scratch / ('go-' + mode + str(len(declaration)))
                    index = StructuralIndex(self.root, output)
                    self.ready(index, ['go.mod', 'main.go', 'other.go'], mode=mode, concurrency=concurrency)
                    call = next(row for row in index.read_facts('sites')
                                if row['path'] == 'main.go' and row['role'] == 'call' and row['text'] == 'Target()')
                    self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
                    self.assertFalse(call['targets_exhaustive'])
                    self.assertIn('ambiguous', call['reason'])

    def test_failed_source_updates_and_cancel_preserve_published_artifact(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\n'})
        index = StructuralIndex(self.root, self.output)
        self.ready(index, ['main.py'])
        retained, metadata = facts(index), index.metadata()
        before = (self.output / 'search.db').read_bytes()
        outside = self.scratch / 'outside.py'
        outside.write_text('def foreign(): return 9\n')
        (self.root / 'main.py').unlink()
        (self.root / 'main.py').symlink_to(outside)
        result = index.refresh(['main.py'])
        self.assertIn(result['status'], ('failed', 'interrupted'))
        self.assertFalse(result['published'])
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        (self.root / 'main.py').unlink()
        write_sources(self.root, {'main.py': 'def target(): return 2\n'})
        read = SourceRoot.read
        changed = False
        def mutate(boundary, path, *args, **kwargs):
            nonlocal changed
            result = read(boundary, path, *args, **kwargs)
            if boundary.root == self.root and path == 'main.py' and not changed:
                changed = True
                write_sources(self.root, {'main.py': 'def target(): return 3\n'})
            return result
        with patch.object(SourceRoot, 'read', mutate):
            result = index.refresh(['main.py'])
        self.assertTrue(changed)
        self.assertIn(result['status'], ('failed', 'interrupted'))
        self.assertFalse(result['published'])
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            with self.subTest(mode=mode):
                result = index.refresh(['main.py'], mode=mode, concurrency=concurrency, cancel=lambda: True)
                self.assertEqual(result['status'], 'interrupted')
                self.assertFalse(result['published'])
                self.assertEqual((self.output / 'search.db').read_bytes(), before)
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata(), metadata)
        self.assertEqual(facts(reopened), retained)


    def test_persisted_sql_occurrence_pages_keep_unknowns_and_all_operation_kinds(self):
        from repo_graph import analysis_queries
        from repo_graph.analysis_queries import SQLSnapshot, Limits, encoded
        def check_handles(page, seed=None, *, symbols=False, maximum=50):
            handles = ({row['id'] for row in page['rows']} if symbols else
                {handle['id'] for row in page['rows'] for handle in
                 (row['caller'], row['target']) if handle is not None})
            self.assertLessEqual(len(handles), maximum)
            self.assertEqual(page['returned_symbol_handles'], len(handles))
            self.assertEqual(page['returned_entities'], len(handles - {seed}))
            return handles
        text = ''.join('def leaf_%02d(): return %d\n' % (i, i) for i in range(40))
        text += 'def hub(callback):\n' + ''.join('    leaf_%02d()\n' % i for i in range(40))
        text += '    callback()\n    return leaf_00\n'
        sources = {'fanout.py': text, 'cycle.py': 'def left(): return right()\ndef right(): return left()\n'}
        write_sources(self.root, sources)
        mode_rows = []
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            with self.subTest(mode=mode):
                index = StructuralIndex(self.root, self.scratch / mode)
                receipt = self.ready(index, sources, mode=mode, concurrency=concurrency)
                declarations = list(index.read_facts('definitions'))
                names = {row['name']: row['id'] for row in declarations}
                retained = list(index.read_facts('sites'))
                with SQLSnapshot(index.output, index.owner, index.output_owner) as snapshot:
                    cursor, rows, pages = None, [], []
                    for _ in range(50):
                        page = snapshot.query(names['hub'], role='all', cursor=cursor,
                            limits=Limits(max_edges=1, max_excerpt_bytes=0))
                        self.assertLessEqual(len(encoded(page)), 32768)
                        self.assertEqual(page['excerpt_bytes'], 0)
                        self.assertEqual(page['generation'], receipt['generation'])
                        check_handles(page, names['hub'])
                        rows.extend(page['rows'])
                        pages.append(page)
                        cursor = page['cursor']
                        if cursor is None:
                            break
                    self.assertIsNone(cursor)
                    self.assertEqual(len(rows), 42)
                    self.assertEqual(len({(row['site']['id'], row['target']['id'] if row['target'] else None) for row in rows}), 42)
                    self.assertEqual(sum(page['examined_relationships'] for page in pages), 42)
                    self.assertEqual(pages[-1]['total_count'], {'value': 42, 'kind': 'exact'})
                    unknown = [row for row in rows if row['certainty'] == 'unresolved']
                    self.assertEqual(len(unknown), 1)
                    self.assertIsNone(unknown[0]['target'])
                    self.assertFalse(unknown[0]['targets_exhaustive'])
                    self.assertTrue(unknown[0]['reason'])
                    for row in rows:
                        actual = next(site for site in retained if site['id'] == row['site']['id'])
                        self.assertEqual(row['site']['range'], actual['range'])
                        self.assertEqual(row['site']['source_sha256'], actual['provenance']['source_sha256'])
                        self.assertEqual(row['site']['role'], actual['role'])
                        self.assertEqual(row['caller']['id'], actual['caller'])
                    mode_rows.append(rows)
                    symbols = snapshot.query(operation='symbol')
                    self.assertEqual(len(symbols['rows']), 43)
                    self.assertEqual(symbols['examined_symbols'], 43)
                    check_handles(symbols, symbols=True)
                    first_calls = snapshot.query(names['hub'], operation='call')
                    self.assertTrue(first_calls['truncated'])
                    self.assertEqual(first_calls['stop_reason'], 'response_byte_budget_exceeded')
                    self.assertLessEqual(len(encoded(first_calls)), 32768)
                    calls = snapshot.query(names['hub'], operation='call', limits=Limits(max_response_bytes=65536))
                    references = snapshot.query(operation='reference')
                    self.assertEqual(len(calls['rows']), 41)
                    self.assertEqual(len(references['rows']), 1)
                    check_handles(calls, names['hub'])
                    check_handles(references)
                    callers = snapshot.query(names['leaf_00'], operation='callers')
                    self.assertEqual(callers['rows'][0]['caller']['id'], names['hub'])
                    check_handles(callers, names['leaf_00'])
                    for operation in ('reachable', 'impact'):
                        cycle = snapshot.query(names['left'], operation=operation, depth=2)
                        self.assertEqual(len(cycle['rows']), 2)
                        self.assertEqual(cycle['returned_entities'], 1)
                        self.assertEqual(cycle['returned_symbol_handles'], 2)
                        check_handles(cycle, names['left'])
                        self.assertIsNone(cycle['cursor'])
                        self.assertEqual(len(snapshot.query(names['left'], operation=operation, depth=1)['rows']), 1)
                    first = snapshot.query(names['hub'], depth=1, limits=Limits(max_edges=1))
                    with patch.object(snapshot, '_row', side_effect=AssertionError('Must reject before materialization')):
                        for change in ({'scope': 'cycle'}, {'prefix': 'leaf'}, {'role': 'all'},
                                       {'operation': 'callers'}, {'depth': 2}):
                            with self.assertRaises(ValueError):
                                snapshot.query(names['hub'], cursor=first['cursor'], **dict({'depth': 1}, **change))
                        with patch.object(analysis_queries, 'QUERY_RULE_VERSION', 'synthetic-version-change'), self.assertRaises(ValueError):
                            snapshot.query(names['hub'], depth=1, cursor=first['cursor'])
                    resumed = snapshot.query(names['hub'], depth=1, cursor=first['cursor'], limits=Limits(max_edges=1))
                    self.assertEqual(resumed['rows'][0], rows[1])
                    with self.assertRaises(ValueError):
                        snapshot.query(names['hub'], depth=1, cursor=first['cursor'])
        self.assertEqual(mode_rows[0], mode_rows[1])

        # Every global occurrence returns both its caller and target declaration.
        disjoint = ''.join('def a%02d(): return %d\ndef b%02d(): return a%02d()\n' %
                           (i, i, i, i) for i in range(30))
        disjoint += 'def unknown(callback): return callback()\n'
        write_sources(self.root, {'disjoint.py': disjoint})
        disjoint_rows = []
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            with self.subTest(mode=mode, coverage='all_returned_handles'):
                index = StructuralIndex(self.root, self.scratch / ('handles-' + mode))
                self.ready(index, ['disjoint.py'], mode=mode, concurrency=concurrency)
                names = {row['name']: row['id'] for row in index.read_facts('definitions')}
                with SQLSnapshot(index.output, index.owner, index.output_owner) as snapshot:
                    page = snapshot.query(operation='call')
                    self.assertEqual(page['stop_reason'], 'entity_budget_exceeded')
                    self.assertEqual(len(page['rows']), 25)
                    self.assertEqual(len(check_handles(page)), 50)
                    rows, work = list(page['rows']), page['examined_relationships']
                    for _ in range(4):
                        if page['cursor'] is None:
                            break
                        page = snapshot.query(operation='call', cursor=page['cursor'])
                        check_handles(page)
                        rows.extend(page['rows'])
                        work += page['examined_relationships']
                    self.assertIsNone(page['cursor'])
                    self.assertEqual(len(rows), 31)
                    self.assertEqual(len({row['site']['id'] for row in rows}), 31)
                    self.assertEqual(work, 31)
                    self.assertEqual(page['total_count'], {'value': 31, 'kind': 'exact'})
                    disjoint_rows.append(rows)

                    unknown = snapshot.query(names['unknown'], operation='call', limits=Limits(max_entities=1))
                    self.assertEqual(check_handles(unknown, names['unknown'], maximum=1), {names['unknown']})
                    self.assertEqual(unknown['returned_entities'], 0)
                    self.assertEqual(len(unknown['rows']), 1)
                    self.assertIsNone(unknown['rows'][0]['target'])
                    self.assertFalse(unknown['rows'][0]['targets_exhaustive'])
                    self.assertEqual(unknown['rows'][0]['certainty'], 'unresolved')
                    self.assertTrue(unknown['rows'][0]['reason'])
                    self.assertIsNone(unknown['cursor'])

                    blocked = snapshot.query(names['b00'], limits=Limits(max_entities=1))
                    self.assertEqual(blocked['stop_reason'], 'entity_budget_exceeded')
                    self.assertEqual(blocked['rows'], [])
                    self.assertEqual(blocked['examined_relationships'], 1)
                    check_handles(blocked, names['b00'], maximum=1)
                    self.assertIsNotNone(blocked['cursor'])
                    with patch.object(snapshot, '_next', side_effect=AssertionError('Pending retry must not reread storage')), \
                            patch.object(snapshot, '_row', side_effect=AssertionError('Pending retry must not rematerialize')):
                        resumed = snapshot.query(names['b00'], cursor=blocked['cursor'],
                            limits=Limits(max_entities=2, max_edges=1))
                    self.assertEqual(resumed['examined_relationships'], 0)
                    self.assertEqual(len(resumed['rows']), 1)
                    self.assertEqual(check_handles(resumed, names['b00'], maximum=2), {names['b00'], names['a00']})
                    self.assertEqual(resumed['rows'][0], rows[0])
                    terminal = snapshot.query(names['b00'], cursor=resumed['cursor'], limits=Limits(max_entities=2))
                    self.assertEqual(terminal['rows'], [])
                    self.assertEqual(terminal['examined_relationships'], 0)
                    self.assertIsNone(terminal['cursor'])
                    self.assertEqual(terminal['total_count'], {'value': 1, 'kind': 'exact'})
                    with self.assertRaises(ValueError):
                        snapshot.query(names['b00'], cursor=blocked['cursor'])
        self.assertEqual(disjoint_rows[0], disjoint_rows[1])

    def test_persisted_sql_storage_cancellation_deadline_and_setup_release(self):
        from repo_graph import analysis_queries
        from repo_graph.analysis_queries import SQLSnapshot, Limits
        write_sources(self.root, {'main.py': ''.join('def leaf_%d(): return %d\n' % (i, i) for i in range(12)) +
                       'def hub(): return leaf_0()\n'})
        expensive = ('SELECT sum(a.start_byte*b.start_byte*c.start_byte*d.start_byte*e.start_byte*f.start_byte) '
            'FROM structural_symbols a CROSS JOIN structural_symbols b CROSS JOIN structural_symbols c '
            'CROSS JOIN structural_symbols d CROSS JOIN structural_symbols e CROSS JOIN structural_symbols f')
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            with self.subTest(mode=mode):
                index = StructuralIndex(self.root, self.scratch / mode)
                self.ready(index, ['main.py'], mode=mode, concurrency=concurrency)
                hub = next(row['id'] for row in index.read_facts('definitions') if row['name'] == 'hub')
                with patch.object(analysis_queries, 'connect', wraps=search.connect) as observed:
                    with self.assertRaises(InterruptedError):
                        SQLSnapshot(index.output, index.owner, index.output_owner, cancel=lambda: True)
                    self.assertEqual(observed.call_count, 0)
                polls = [0]
                def setup_cancel():
                    polls[0] += 1
                    return polls[0] > 5
                with self.assertRaises(InterruptedError):
                    SQLSnapshot(index.output, index.owner, index.output_owner, cancel=setup_cancel)
                self.assertFalse(search.SNAPSHOT_LOCK.locked())
                with SQLSnapshot(index.output, index.owner, index.output_owner) as snapshot:
                    stopped = snapshot.query(hub, cancel=lambda: True)
                    self.assertEqual(stopped['examined_relationships'], 0)
                    self.assertEqual(stopped['stop_reason'], 'cancelled')
                    self.assertIsNone(stopped['cursor'])
                    polls[0] = 0
                    def cancel():
                        polls[0] += 1
                        return polls[0] > 20
                    with patch.object(snapshot, '_next', side_effect=lambda *args: snapshot._read(expensive)):
                        cancelled = snapshot.query(hub, cancel=cancel)
                        expired = snapshot.query(hub, limits=Limits(timeout_seconds=.002))
                    for page, reason in ((cancelled, 'cancelled'), (expired, 'deadline_exceeded')):
                        self.assertEqual(page['stop_reason'], reason)
                        self.assertGreater(page['storage_progress_callbacks'], 0)
                        self.assertEqual(page['rows'], [])
                        self.assertIsNone(page['cursor'])
                        self.assertEqual(page['total_count'], {'value': 0, 'kind': 'lower_bound'})
                    self.assertEqual(len(snapshot.query(hub)['rows']), 1)


class StructuralValidationTests(unittest.TestCase):
    def test_structural_reporting_preserves_other_tasks_and_replaces_stale_success(self):
        from evaluations import analysis
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            path = root / analysis.FACTS_OUTPUT
            path.parent.mkdir(parents=True)
            retained = {'status': 'passed', 'source_identity': 'retained-owner-proof',
                        'case_results': [{'id': 'schema', 'status': 'passed'}]}
            result = {'status': 'passed', 'source_identity': 'synthetic-construct-key',
                'counts': {'checks': 1}, 'case_results': [{'id': 'receiver', 'status': 'passed'}],
                'failures': [], 'coverage_failures': [{'id': 'receiver', 'status': 'failed',
                    'dimension': 'receiver_target_enumeration'}]}
            for suite, task in (('constructs', 'T010'), ('incremental', 'T011'), ('queries', 'T012'), ('coverage', 'T013'),
                                ('evidence', 'T014')):
                prior = {'T009': retained, 'T010': {'status': 'passed', 'source_identity': 'retained-construct-proof'},
                         'T011': {'status': 'passed', 'source_identity': 'retained-update-proof'},
                         'T012': {'status': 'passed', 'source_identity': 'retained-query-proof'},
                         'T013': {'status': 'passed', 'source_identity': 'retained-coverage-proof'}}
                path.write_text(json.dumps({'schema_version': 1, 'tasks': prior}))
                with self.subTest(suite=suite), patch.object(analysis, 'ROOT', root), \
                        patch.object(analysis, suite, return_value=dict(result)), redirect_stdout(io.StringIO()):
                    self.assertEqual(analysis.main(['--suite', suite]), 0)
                report = json.loads(path.read_text())
                self.assertEqual(report['tasks']['T009'], retained)
                self.assertEqual(report['tasks'][task]['coverage_failures'], result['coverage_failures'])
                for other in prior.keys() - {task}:
                    self.assertEqual(report['tasks'][other], prior[other])
                for error in (ValueError('invalid frozen source'), RuntimeError('unexpected producer failure')):
                    path.write_text(json.dumps({'schema_version': 1, 'tasks': dict(prior, **{task: result})}))
                    with self.subTest(suite=suite, error=type(error).__name__), patch.object(analysis, 'ROOT', root), \
                            patch.object(analysis, suite, side_effect=error), redirect_stdout(io.StringIO()):
                        self.assertEqual(analysis.main(['--suite', suite]), 1)
                    report = json.loads(path.read_text())
                    self.assertEqual(report['tasks']['T009'], retained)
                    self.assertEqual(report['tasks'][task]['status'], 'failed')
                    self.assertEqual(report['tasks'][task]['error_kind'], type(error).__name__)
                    for other in prior.keys() - {task}:
                        self.assertEqual(report['tasks'][other], prior[other])
                if task in ('T012', 'T013', 'T014'):
                    alternate = root / 'query-check.json'
                    alternate.write_text(json.dumps(result))
                    saved = path.read_bytes()
                    with patch.object(analysis, 'ROOT', root), \
                            patch.object(analysis, suite, side_effect=RuntimeError('unexpected alternate-output failure')), \
                            redirect_stdout(io.StringIO()):
                        self.assertEqual(analysis.main(['--suite', suite, '--output', alternate.name]), 1)
                    self.assertEqual(json.loads(alternate.read_text())['status'], 'failed')
                    self.assertEqual(path.read_bytes(), saved)

    def test_invalid_inventory_queue_and_limit_arguments_are_rejected(self):
        with tempfile.TemporaryDirectory() as scratch:
            root, output = Path(scratch) / 'repo', Path(scratch) / 'out'
            root.mkdir()
            write_sources(root, {'main.py': 'def target(): return 1\n'})
            index = StructuralIndex(root, output)
            malformed = [
                ['main.py', 'main.py'], ['/main.py'], ['../main.py'], ['./main.py'],
                ['dir//main.py'], ['dir\\main.py'], [''], [True], 'main.py', [{}],
                [{'path': 'main.py', 'content': b'not metadata'}],
                [{'path': 'main.py', 'language': True}],
                [{'path': 'main.py', 'sha256': 'invalid'}],
                [{'path': 'main.py', 'bytes': True}],
                [{'path': 'main.py', 'bytes': -1}],
                [{'path': 'main.py', 'kind': 'invented'}],
            ]
            for inventory in malformed:
                with self.subTest(inventory=inventory), self.assertRaises((ValueError, OSError)):
                    index.refresh(inventory)
            arguments = [dict(mode='invented'), dict(concurrency=0), dict(concurrency=True),
                         dict(mode='queued', concurrency=5), dict(mode='serial', concurrency=2)]
            for options in arguments:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    index.refresh(['main.py'], **options)
            for options in (dict(max_files=0), dict(batch_files=True), dict(max_source_bytes=-1),
                            dict(max_index_bytes=0), dict(total_wall_seconds=float('inf'))):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    IndexLimits(**options)
            self.assertFalse((output / 'search.db').exists())


if __name__ == '__main__':
    unittest.main()
