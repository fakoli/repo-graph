#!/usr/bin/env python3
"""Native Pi initialization and RPC bash checks in a provider-free home."""
import json
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import sys
import tempfile
import time


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='repo-graph-pi-') as scratch:
        home = Path(scratch) / 'home'; agent = home / '.pi/agent'; agent.mkdir(parents=True)
        repo = Path(scratch) / 'caller source'; repo.mkdir()
        output = Path(scratch) / 'map output'
        (repo / 'queue.py').write_text('def enqueue():\n    """Schedule work for later."""\n')
        settings = agent / 'settings.json'
        preserved = {'defaultProvider': 'openai', 'defaultModel': 'synthetic-preserved-model',
                     'enableInstallTelemetry': False}
        settings.write_text(json.dumps(preserved))
        env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'PI_CODING_AGENT_DIR': str(agent),
               'PI_OFFLINE': '1', 'PI_TELEMETRY': '0', 'GIT_TERMINAL_PROMPT': '0'}
        command = [sys.executable, str(root / 'scripts/repo_graph.py'), 'init', '--harness', 'pi', '--source', str(root)]
        for _ in range(2):
            initialized = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=60)
            assert initialized.returncode == 0, initialized.stderr
            assert json.loads(initialized.stdout)['harnesses'][0]['status'] == 'installed'
        config = json.loads(settings.read_text())
        assert all(config[key] == value for key, value in preserved.items())
        sources = [item['source'] if isinstance(item, dict) else item for item in config['packages']]
        assert [(agent / item).resolve() for item in sources].count(root) == 1
        child = subprocess.Popen(['pi', '--mode', 'rpc', '--offline', '--no-session', '--no-extensions',
            '--no-context-files', '--no-prompt-templates', '--no-themes'], cwd=repo, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        serial = 0; selector = selectors.DefaultSelector(); selector.register(child.stdout, selectors.EVENT_READ)

        def request(kind, **extra):
            nonlocal serial
            serial += 1
            child.stdin.write(json.dumps({'type': kind, 'id': str(serial), **extra}) + '\n'); child.stdin.flush()
            deadline = time.monotonic() + 30
            while selector.select(timeout=max(0, deadline - time.monotonic())):
                line = child.stdout.readline()
                if not line: break
                value = json.loads(line)
                if value.get('id') == str(serial) and value.get('type') == 'response':
                    assert value['success'], value
                    return value['data']
            raise RuntimeError('Pi RPC response timeout')

        try:
            commands = request('get_commands')['commands']
            skill = next(value for value in commands if value['name'] == 'skill:repo-graph')
            skill_path = Path(skill['sourceInfo']['path'])
            assert skill_path.read_bytes() == (root / 'skills/repo-graph/SKILL.md').read_bytes()
            script = shlex.quote(str((skill_path.parent / '../../scripts/repo_graph.py').resolve()))
            for repeated in (False, True):
                mapped = request('bash', command=f'{shlex.quote(sys.executable)} {script} map --output {shlex.quote(str(output))}')
                assert mapped['exitCode'] == 0, mapped
                graph = json.loads((output / 'graph.json').read_text())
                assert graph['name'] == 'caller source' and graph['files'] == ['queue.py']
                assert graph['scan']['reused'] == graph['search']['reused'] == int(repeated)
                assert graph['search']['documents'] == 1 and graph['jev'] == 'off'
                for file in ('architecture.html', 'graph.html', 'architecture.mmd', 'architecture.md', 'search.db'):
                    assert (output / file).stat().st_size > 0
            found = request('bash', command=f'{shlex.quote(sys.executable)} {script} search {shlex.quote(str(output))} enqueue --mode keyword --limit 1')
            assert found['exitCode'] == 0, found
            assert json.loads(found['output'])['results'][0]['path'] == 'queue.py'
            for destination in ('.', 'inside output'):
                rejected = request('bash', command=f'{shlex.quote(sys.executable)} {script} map --output {shlex.quote(destination)}')
                assert rejected['exitCode'] != 0, rejected
            print('Pi native init/repeat preserves providers; discovery, caller-CWD map, artifacts, cache reuse, keyword search and output rejection passed; no provider calls')
        finally:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
            selector.close()


if __name__ == '__main__':
    main()
