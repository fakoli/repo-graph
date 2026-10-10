#!/usr/bin/env python3
"""Native Codex/Claude install and packaged-script execution in isolated homes."""
import argparse
import json
import os
from pathlib import Path
import shutil
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
        product = Path(scratch) / 'product source'
        product.mkdir()
        for directory in ('repo_graph', 'scripts', 'skills', '.agents', '.codex-plugin', '.claude-plugin'):
            shutil.copytree(source / directory, product / directory,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copyfile(source / 'package.json', product / 'package.json')
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

        # Version changes carry a marker so a stale native cache cannot pass.
        for version in ('0.6.0', '0.6.0', '0.6.1', '0.6.0'):
            for manifest in ('package.json', '.codex-plugin/plugin.json', '.claude-plugin/plugin.json'):
                path = product / manifest
                data = json.loads(path.read_text()); data['version'] = version
                path.write_text(json.dumps(data))
            (product / 'selected-version.txt').write_text(version)
            run([sys.executable, str(source / 'scripts/repo_graph.py'), 'init',
                 '--harness', args.harness, '--source', str(product)])
            plugins = json.loads(run([args.harness, 'plugin', 'list', '--json']))
            if args.harness == 'codex':
                installed = [p for p in plugins['installed']
                             if p['pluginId'] == 'repo-graph@repo-graph']
                packaged = codex / 'plugins/cache/repo-graph/repo-graph' / version
            else:
                installed = [p for p in plugins if p['id'] == 'repo-graph@repo-graph']
                packaged = Path(installed[0]['installPath'])
            assert len(installed) == 1 and installed[0]['enabled'], installed
            assert installed[0]['version'] == version, installed
            assert (packaged / 'selected-version.txt').read_text() == version
            assert (packaged / 'skills/repo-graph/SKILL.md').read_bytes() == (
                source / 'skills/repo-graph/SKILL.md').read_bytes()
        if args.harness == 'claude':
            details = run(['claude', 'plugin', 'details', 'repo-graph'])
            assert 'Skills (1)' in details and 'repo-graph' in details, details
        script = packaged / 'scripts/repo_graph.py'
        run([sys.executable, str(script), 'map', '--output', str(output)])
        assert json.loads((output / 'graph.json').read_text())['files'] == ['queue.py']
        found = json.loads(run([sys.executable, str(script), 'search', str(output),
                               'enqueue', '--mode', 'keyword', '--limit', '1']))
        assert [p['path'] for p in found['results']] == ['queue.py'], found
        if args.harness == 'codex':
            (repo / 'test_queue.py').write_text('from queue import enqueue\n\ndef test_enqueue():\n    assert enqueue() is None\n')
            cache = Path(scratch) / 'review output'
            policy = repo / 'repo-graph-review.json'
            for untrusted in ({'include': ['queue.py']}, {'scope': '.'}):
                policy.write_text(json.dumps(untrusted))
                refused = subprocess.run([sys.executable, str(script), 'review', 'plan', '.',
                                         '--scope', 'test_queue.py', '--output', str(cache)],
                                        cwd=repo, env=env, capture_output=True, text=True, timeout=60)
                assert refused.returncode == 1 and not cache.exists(), refused.stderr
            policy.write_text(json.dumps({'scope': 'test_queue.py', 'limits': {'result_bytes': 4096}}))
            planned = json.loads(run([sys.executable, str(script), 'review', 'plan', '.',
                                      '--scope', 'test_queue.py', '--output', str(cache)]))
            campaign = planned['campaign']
            assigned = json.loads(run([sys.executable, str(script), 'review', 'next', campaign,
                                       '--worker', 'synthetic-worker']))
            packet, attempt = assigned['packet'], assigned['attempt']
            assert assigned['result_schema']['worker_result']['limits']['bytes'] == 4096
            sources = [{key: item[key] for key in ('path', 'start_line', 'end_line', 'sha256')}
                       for item in packet['sources']]
            result = dict(packet_id=packet['packet_id'], attempt_id=attempt['attempt_id'],
                          worker_id=attempt['worker_id'], reviewed_ranges=sources, findings=[],
                          assertion_map=[dict(original='test_queue.py:4', disposition='preserved',
                                              evidence='The original enqueue assertion remains.')],
                          outcome='completed')
            result_file = Path(scratch) / 'synthetic-result.json'
            result_file.write_text(json.dumps(result))
            recorded = json.loads(run([sys.executable, str(script), 'review', 'record', campaign,
                                       '--result', str(result_file)]))
            assert recorded['state'] == 'validated', recorded
            decision = dict(kind='independent_decision', packet_id=packet['packet_id'],
                            attempt_id=attempt['attempt_id'], result_sha256=recorded['sha256'],
                            reviewer_id='synthetic-independent-reviewer', disposition='accepted',
                            provenance=dict(model='not_used', surface='provider-free-smoke', reasoning='not_used'),
                            rationale='Synthetic transport check only; no semantic review or model qualification.')
            result_file.write_text(json.dumps(decision))
            accepted = json.loads(run([sys.executable, str(script), 'review', 'record', campaign,
                                       '--result', str(result_file)]))
            assert accepted['state'] == 'accepted', accepted
            resumed = json.loads(run([sys.executable, str(script), 'review', 'status', campaign]))
            assert resumed['packets'][0]['state'] == 'accepted', resumed
            assert json.loads(run([sys.executable, str(script), 'review', 'next', campaign]))['packet'] is None
            print('codex: installed packet/result/resume passed with synthetic results; no model qualification claimed')
        print(f'{args.harness}: native install/repeat/upgrade/rollback, shared skill, caller map and search passed')


if __name__ == '__main__':
    main()
