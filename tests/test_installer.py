from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_graph import __version__, cli, installer


class InstallerTests(unittest.TestCase):
    def product(self, root, version='0.6.0'):
        for file in installer.MANIFESTS.values():
            path = root / file; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('{}')
        (root / 'package.json').write_text(json.dumps(dict(name='repo-graph-agent', version=version)))
        for file in installer.PLUGIN_MANIFESTS.values():
            path = root / file; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(name='repo-graph', version=version)))
        for file in ('scripts/repo_graph.py', 'skills/repo-graph/SKILL.md', 'repo_graph/__init__.py', 'repo_graph/assets/view.js'):
            path = root / file; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('synthetic product ' + version)

    def native(self, name, source, cached, *, existing=False, scope='user', remote=False, revision='v0.6.0'):
        state = dict(existing=existing)

        def run(command, **kwargs):
            output = ''
            if command[0] == 'git':
                output = 'selected-sha\n'
            elif command[1:4] == ['plugin', 'marketplace', 'list']:
                if name == 'codex':
                    registered = dict(sourceType='git' if remote else 'local', source=f'https://github.com/{installer.REPOSITORY}.git' if remote else str(source))
                    output = json.dumps(dict(marketplaces=[dict(name='repo-graph', root=str(source), marketplaceSource=registered)]))
                else:
                    registered = dict(name='repo-graph', source='git' if remote else 'directory', installLocation=str(source))
                    registered.update(dict(url=f'https://github.com/{installer.REPOSITORY}.git', ref=revision) if remote else dict(path=str(source)))
                    output = json.dumps([registered])
            elif command[1:3] == ['plugin', 'list']:
                plugin = dict(version=json.loads((cached / installer.PLUGIN_MANIFESTS[name]).read_text())['version'], enabled=True)
                if name == 'claude':
                    plugin.update(id=installer.PLUGIN_ID, scope=scope, installPath=str(cached))
                    if scope == 'project': plugin['projectPath'] = str(Path.cwd())
                    output = json.dumps([plugin] if state['existing'] else [])
                else:
                    plugin['pluginId'] = installer.PLUGIN_ID
                    output = json.dumps(dict(installed=[plugin]))
            elif command[1:3] == ['plugin', 'add']:
                output = json.dumps(dict(installedPath=str(cached)))
            elif command[1:3] in (['plugin', 'install'], ['plugin', 'update']):
                state['existing'] = True
                if command[2] == 'update': shutil.copytree(source, cached, dirs_exist_ok=True)
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')

        return run

    def test_pinned_native_plan_and_cli_entrypoint(self):
        with patch.object(installer.shutil, 'which', side_effect=lambda name: name), patch.object(installer.subprocess, 'run') as run:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(['init', '--dry-run']), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt['ref'], f'v{__version__}')
            self.assertEqual([plan['harness'] for plan in receipt['harnesses']], ['pi', 'codex', 'claude'])
            self.assertEqual(receipt['harnesses'][0]['commands'], [['pi', 'install', f'git:github.com/fakoli/repo-graph@v{__version__}']])
            self.assertEqual(receipt['harnesses'][1]['commands'][0][-2:], ['--ref', f'v{__version__}'])
            self.assertEqual(receipt['harnesses'][2]['commands'][0][-3:], [f'https://github.com/fakoli/repo-graph.git#v{__version__}', '--scope', 'user'])
            run.assert_not_called()

    def test_preflight_rejects_missing_cli_unsupported_scope_and_bad_sources_before_install(self):
        with patch.object(installer.subprocess, 'run') as run:
            with patch.object(installer.shutil, 'which', side_effect=lambda name: None if name == 'claude' else name):
                with self.assertRaisesRegex(RuntimeError, 'claude'):
                    installer.initialize()
            for harness in ('codex', 'all'):
                with self.assertRaisesRegex(ValueError, 'user scope only'):
                    installer.initialize(harness=harness, scope='project')
            with self.assertRaises(ValueError):
                installer.initialize(harness='pi', ref='v0.6; unsafe')
            with tempfile.TemporaryDirectory() as scratch:
                with self.assertRaisesRegex(ValueError, 'missing package.json'):
                    installer.initialize(harness='pi', source=Path(scratch))
                with self.assertRaisesRegex(ValueError, '--ref applies'):
                    installer.initialize(harness='pi', source=Path(scratch), ref='v0.6.0')
                root = Path(scratch) / 'invalid'; self.product(root)
                for harness, file in [('pi', 'package.json'), ('codex', installer.PLUGIN_MANIFESTS['codex']), ('claude', installer.PLUGIN_MANIFESTS['claude'])]:
                    self.product(root)
                    (root / file).write_text(json.dumps(dict(name='unrelated', version='1.0.0')))
                    with self.assertRaisesRegex(ValueError, 'Local product needs'):
                        installer.initialize(harness=harness, source=root)
                self.product(root)
                (root / 'scripts/repo_graph.py').unlink()
                with self.assertRaisesRegex(ValueError, 'runtime/script/skill'):
                    installer.initialize(harness='codex', source=root)
            run.assert_not_called()

    def test_local_project_scope_and_partial_failure_are_reported(self):
        with tempfile.TemporaryDirectory(prefix='repo graph init ') as scratch:
            root = Path(scratch) / 'product'; self.product(root)
            with patch.object(installer.shutil, 'which', side_effect=lambda name: name), patch.object(installer.subprocess, 'run') as run:
                result = installer.initialize(harness='pi', scope='project', source=root)
                self.assertEqual(result['harnesses'][0]['status'], 'installed')
                self.assertEqual(run.call_args.args[0], ['pi', 'install', str(root), '--local'])
                run.reset_mock()
                def fail(command, **kwargs):
                    if command[0] == 'pi': return None
                    if command[1:4] == ['plugin', 'marketplace', 'list']:
                        return subprocess.CompletedProcess(command, 0, stdout='{"marketplaces": []}')
                    raise subprocess.CalledProcessError(2, command, stderr='x' * 3000 + 'unsupported flag')
                run.side_effect = fail
                with self.assertRaisesRegex(RuntimeError, 'completed harnesses: pi') as caught:
                    installer.initialize(source=root)
                self.assertIn('step 1/2 (exit 2)', str(caught.exception))
                self.assertTrue(str(caught.exception).endswith('unsupported flag'))
                self.assertLess(len(str(caught.exception)), 2300)
                self.assertEqual(run.call_count, 3)
                self.assertEqual(run.call_args.args[0][0], 'codex')

    def test_claude_initial_install_and_existing_update_verify_cached_product(self):
        with tempfile.TemporaryDirectory(prefix='repo graph init ') as scratch:
            source = Path(scratch) / 'product'; self.product(source)
            cached = Path(scratch) / 'cached'; shutil.copytree(source, cached)
            with patch.object(installer.shutil, 'which', side_effect=lambda name: name):
                for existing in (False, True):
                    if existing: self.product(cached, '0.5.0')
                    with patch.object(installer.subprocess, 'run', side_effect=self.native('claude', source, cached, existing=existing, scope='project')) as run:
                        receipt = installer.initialize(harness='claude', source=source, scope='project')
                    plan = receipt['harnesses'][0]
                    self.assertEqual(plan['commands'][1][2], 'update' if existing else 'install')
                    self.assertTrue(all(command[-2:] == ['--scope', 'project'] for command in plan['commands']))
                    self.assertEqual(plan['verification']['version'], '0.6.0')
                    self.assertTrue(any(call.args[0][1:3] == ['plugin', 'list'] for call in run.call_args_list))

    def test_native_success_cannot_claim_stale_version_source_or_files(self):
        with tempfile.TemporaryDirectory(prefix='repo graph init ') as scratch:
            source = Path(scratch) / 'product'; self.product(source)
            cached = Path(scratch) / 'cached'; shutil.copytree(source, cached)
            with patch.object(installer.shutil, 'which', side_effect=lambda name: name):
                for mismatch, expected in [('version', 'selected version'), ('files', 'differs from selected source'), ('source', 'selected local source')]:
                    self.product(cached)
                    if mismatch == 'version': self.product(cached, '0.5.0')
                    if mismatch == 'files': (cached / 'repo_graph/assets/view.js').write_text('stale cached content')
                    native = self.native('codex', source, cached)

                    def run(command, **kwargs):
                        result = native(command, **kwargs)
                        if mismatch == 'source' and command[1:4] == ['plugin', 'marketplace', 'list']:
                            data = json.loads(result.stdout)
                            data['marketplaces'][0]['marketplaceSource']['source'] = str(source.parent)
                            result.stdout = json.dumps(data)
                        return result

                    with self.subTest(mismatch=mismatch), patch.object(installer.subprocess, 'run', side_effect=run):
                        with self.assertRaisesRegex(RuntimeError, 'verification.*completed harnesses: none') as caught:
                            installer.initialize(harness='codex', source=source)
                        self.assertIn(expected, str(caught.exception))

    def test_remote_pin_is_checked_for_each_native_plugin_manager(self):
        with tempfile.TemporaryDirectory(prefix='repo graph init ') as scratch:
            source = Path(scratch) / 'snapshot'; self.product(source)
            cached = Path(scratch) / 'cached'; shutil.copytree(source, cached)
            with patch.object(installer.shutil, 'which', side_effect=lambda name: name):
                for name in ('codex', 'claude'):
                    native = self.native(name, source, cached, remote=True)
                    with patch.object(installer.subprocess, 'run', side_effect=native):
                        receipt = installer.initialize(harness=name)
                    self.assertEqual(receipt['harnesses'][0]['verification']['ref'], 'v0.6.0')

                    def wrong_ref(command, **kwargs):
                        result = native(command, **kwargs)
                        if name == 'codex' and command[-1].endswith('^{commit}'):
                            result.stdout = 'old-sha\n'
                        elif name == 'claude' and command[1:4] == ['plugin', 'marketplace', 'list']:
                            data = json.loads(result.stdout); data[0]['ref'] = 'v0.5.0'; result.stdout = json.dumps(data)
                        return result

                    with self.subTest(harness=name), patch.object(installer.subprocess, 'run', side_effect=wrong_ref):
                        with self.assertRaisesRegex(RuntimeError, 'selected ref'):
                            installer.initialize(harness=name)

    def test_codex_ref_replacement_prevalidates_before_native_removal(self):
        with tempfile.TemporaryDirectory(prefix='repo graph init ') as scratch:
            source = Path(scratch) / 'snapshot'; self.product(source)
            cached = Path(scratch) / 'cached'; shutil.copytree(source, cached)
            native = self.native('codex', source, cached, remote=True)
            conflicted = False

            def run(command, **kwargs):
                nonlocal conflicted
                if command[1:4] == ['plugin', 'marketplace', 'add'] and not conflicted:
                    conflicted = True
                    raise subprocess.CalledProcessError(1, command, stderr='already added from a different source')
                if command[0] == 'git' and 'show' in command:
                    data = dict(name='repo-graph', version='0.6.0')
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(data))
                return native(command, **kwargs)

            with patch.object(installer.shutil, 'which', side_effect=lambda name: name), patch.object(installer.subprocess, 'run', side_effect=run) as calls:
                receipt = installer.initialize(harness='codex')
            commands = [call.args[0] for call in calls.call_args_list]
            fetch = next(i for i, command in enumerate(commands) if command[0] == 'git' and 'fetch' in command)
            removal = next(i for i, command in enumerate(commands) if command[1:4] == ['plugin', 'marketplace', 'remove'])
            self.assertLess(fetch, removal)
            self.assertEqual(receipt['harnesses'][0]['commands'][0], ['codex', 'plugin', 'marketplace', 'remove', 'repo-graph'])

            conflicted = False

            def invalid_ref(command, **kwargs):
                if command[0] == 'git' and 'fetch' in command:
                    raise subprocess.CalledProcessError(1, command, stderr='requested ref missing')
                return run(command, **kwargs)

            with patch.object(installer.shutil, 'which', side_effect=lambda name: name), patch.object(installer.subprocess, 'run', side_effect=invalid_ref) as calls:
                with self.assertRaisesRegex(RuntimeError, 'replacement preflight.*completed harnesses: none'):
                    installer.initialize(harness='codex')
            self.assertFalse(any(call.args[0][1:4] == ['plugin', 'marketplace', 'remove'] for call in calls.call_args_list))

    def test_unrelated_marketplace_name_is_preserved(self):
        unrelated = dict(name='repo-graph', root='/synthetic/snapshot', marketplaceSource=dict(sourceType='git', source='https://example.invalid/unrelated.git'))
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(dict(marketplaces=[unrelated])))
        with patch.object(installer.shutil, 'which', side_effect=lambda name: name), patch.object(installer.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, 'belongs to another source'):
                installer.initialize(harness='codex')
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__': unittest.main()
