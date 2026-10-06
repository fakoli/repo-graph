#!/usr/bin/env python3
"""Real Pi package discovery and native bash execution, no provider requests."""
import json
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='repo-graph-pi-') as scratch:
    home = Path(scratch)/'home'; agent = home/'.pi/agent'; agent.mkdir(parents=True)
    repo = Path(scratch)/'caller source'; repo.mkdir()
    output = Path(scratch)/'map output'
    (repo/'queue.py').write_text('def enqueue():\n    """Schedule work for later."""\n')
    (agent/'settings.json').write_text(json.dumps({'packages':[str(root)]}))
    env = {'PATH':os.environ['PATH'],'HOME':str(home),'PI_CODING_AGENT_DIR':str(agent),'PI_OFFLINE':'1'}
    child = subprocess.Popen(['pi','--mode','rpc','--offline','--no-session','--no-extensions','--no-context-files','--no-prompt-templates','--no-themes'],cwd=repo,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    serial = 0; selector = selectors.DefaultSelector(); selector.register(child.stdout,selectors.EVENT_READ)
    def request(kind, **extra):
        global serial
        serial += 1
        child.stdin.write(json.dumps({'type':kind,'id':str(serial),**extra})+'\n'); child.stdin.flush()
        while selector.select(timeout=30):
            line = child.stdout.readline()
            if not line: break
            value = json.loads(line)
            if value.get('id')==str(serial) and value.get('type')=='response':
                assert value['success'],value
                return value['data']
        raise RuntimeError('Pi RPC response timeout')
    try:
        commands = request('get_commands')['commands']
        skill = next(value for value in commands if value['name']=='skill:repo-graph')
        path = Path(skill['sourceInfo']['path']).parent/'../../scripts/repo_graph.py'
        script = shlex.quote(str(path.resolve()))
        mapped = request('bash',command=f'python3 {script} map --output {shlex.quote(str(output))}')
        assert mapped['exitCode']==0,mapped
        graph = json.loads((output/'graph.json').read_text())
        assert graph['files']==['queue.py']
        found = request('bash',command=f'python3 {script} search {shlex.quote(str(output))} enqueue --mode keyword --limit 1')
        assert found['exitCode']==0,found
        assert json.loads(found['output'])['results'][0]['path']=='queue.py'
        print('Pi skill discovery, caller-directory map and bounded keyword search passed; no provider calls')
    finally:
        child.terminate()
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
        selector.close()
