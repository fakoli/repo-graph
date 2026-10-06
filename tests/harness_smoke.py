#!/usr/bin/env python3
"""Native Codex/Claude install and packaged-script execution in isolated homes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', choices=['codex', 'claude'], required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    source = args.source.resolve()
    with tempfile.TemporaryDirectory(prefix='repo-graph-harness-') as scratch:
        home = Path(scratch) / 'home'
        codex = home / '.codex'
        claude = home / '.claude'
        codex.mkdir(parents=True)
        claude.mkdir()
        repo = Path(scratch) / 'caller source'
        repo.mkdir()
        output = Path(scratch) / 'map output'
        (repo / 'queue.py').write_text('def enqueue():\n    """Schedule work."""\n')
        env = {'PATH': os.environ['PATH'], 'HOME': str(home),
               'CODEX_HOME': str(codex), 'CLAUDE_CONFIG_DIR': str(claude),
               'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'DISABLE_AUTOUPDATER': '1', 'GIT_TERMINAL_PROMPT': '0'}

        def run(command):
            result = subprocess.run(command, cwd=repo, env=env, capture_output=True,
                                    text=True, timeout=60)
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        run([sys.executable, str(source / 'scripts/repo_graph.py'), 'init',
             '--harness', args.harness, '--source', str(source)])
        # Repeat the public command: native managers must keep one enabled product.
        run([sys.executable, str(source / 'scripts/repo_graph.py'), 'init',
             '--harness', args.harness, '--source', str(source)])
        plugins = json.loads(run([args.harness, 'plugin', 'list', '--json']))
        if args.harness == 'codex':
            installed = [p for p in plugins['installed']
                         if p['pluginId'] == 'repo-graph@repo-graph']
            roots = list((codex / 'plugins/cache/repo-graph/repo-graph').glob('*'))
        else:
            installed = [p for p in plugins if p['id'] == 'repo-graph@repo-graph']
            roots = [Path(p['installPath']) for p in installed]
            details = run(['claude', 'plugin', 'details', 'repo-graph'])
            assert 'Skills (1)' in details and 'repo-graph' in details, details
        assert len(installed) == 1 and installed[0]['enabled'], installed
        packaged = next(p for p in roots if (p / 'skills/repo-graph/SKILL.md').is_file())
        assert (packaged / 'skills/repo-graph/SKILL.md').read_bytes() == (
            source / 'skills/repo-graph/SKILL.md').read_bytes()
        script = packaged / 'scripts/repo_graph.py'
        run([sys.executable, str(script), 'map', '--output', str(output)])
        assert json.loads((output / 'graph.json').read_text())['files'] == ['queue.py']
        found = json.loads(run([sys.executable, str(script), 'search', str(output),
                               'enqueue', '--mode', 'keyword', '--limit', '1']))
        assert [p['path'] for p in found['results']] == ['queue.py'], found
        print(f'{args.harness}: native install/repeat, shared skill, caller map and search passed')


if __name__ == '__main__':
    main()
