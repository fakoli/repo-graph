from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_graph import __version__, cli, installer


class InstallerTests(unittest.TestCase):
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
            run.assert_not_called()

    def test_local_project_scope_and_partial_failure_are_reported(self):
        with tempfile.TemporaryDirectory(prefix='repo graph init ') as scratch:
            root = Path(scratch)
            for file in installer.MANIFESTS.values():
                path = root / file; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('{}')
            with patch.object(installer.shutil, 'which', side_effect=lambda name: name), patch.object(installer.subprocess, 'run') as run:
                result = installer.initialize(harness='pi', scope='project', source=root)
                self.assertEqual(result['harnesses'][0]['status'], 'installed')
                self.assertEqual(run.call_args.args[0], ['pi', 'install', str(root), '--local'])
                result = installer.initialize(harness='claude', scope='project', source=root)
                self.assertTrue(all(command[-2:] == ['--scope', 'project'] for command in result['harnesses'][0]['commands']))
                run.reset_mock()
                run.side_effect = [None, subprocess.CalledProcessError(2, ['codex'], stderr='x' * 3000 + 'unsupported flag')]
                with self.assertRaisesRegex(RuntimeError, 'completed harnesses: pi') as caught:
                    installer.initialize(source=root)
                self.assertIn('step 1/2 (exit 2)', str(caught.exception))
                self.assertTrue(str(caught.exception).endswith('unsupported flag'))
                self.assertLess(len(str(caught.exception)), 2300)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0][0], 'codex')


if __name__ == '__main__': unittest.main()
