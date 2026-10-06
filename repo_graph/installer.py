"""Initialize the canonical product through each harness's native manager."""
from pathlib import Path
import re
import shutil
import subprocess

from . import __version__

HARNESSES = ('pi', 'codex', 'claude')
MARKETPLACE = 'repo-graph'
REPOSITORY = 'fakoli/repo-graph'
MANIFESTS = {'pi': 'package.json', 'codex': '.agents/plugins/marketplace.json',
             'claude': '.claude-plugin/marketplace.json'}


def initialize(*, harness='all', scope='user', source=None, ref=None, dry_run=False):
    if harness not in (*HARNESSES, 'all') or scope not in {'user', 'project'}:
        raise ValueError('Choose pi, codex, claude, or all and user or project scope')
    targets = HARNESSES if harness == 'all' else (harness,)
    if scope == 'project' and 'codex' in targets:
        raise ValueError('Codex native plugin installation supports user scope only. Choose pi or claude for project scope.')
    if source is not None and ref is not None:
        raise ValueError('--ref applies to the remote product; omit it with --source')
    revision = ref or f'v{__version__}'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}', revision):
        raise ValueError('Use a release tag, branch, or commit without URL credentials or shell syntax')
    local = Path(source).expanduser().resolve() if source is not None else None
    if local is not None:
        if not local.is_dir():
            raise ValueError('--source must be a local product directory')
        for name in targets:
            if not (local / MANIFESTS[name]).is_file():
                raise ValueError(f'Local product is missing {MANIFESTS[name]}')
    binaries = {name: shutil.which(name) for name in targets}
    missing = [name for name, binary in binaries.items() if binary is None]
    if missing:
        raise RuntimeError('Install the native harness CLI first: ' + ', '.join(missing))
    plans = []
    for name in targets:
        binary = binaries[name]
        if name == 'pi':
            commands = [[binary, 'install', str(local) if local else f'git:github.com/{REPOSITORY}@{revision}']]
            if scope == 'project':
                commands[0].append('--local')
        elif name == 'codex':
            commands = [[binary, 'plugin', 'marketplace', 'add', str(local) if local else REPOSITORY],
                        [binary, 'plugin', 'add', f'repo-graph@{MARKETPLACE}']]
            if not local:
                commands[0].extend(['--ref', revision])
        else:
            commands = [[binary, 'plugin', 'marketplace', 'add',
                         str(local) if local else f'https://github.com/{REPOSITORY}.git#{revision}', '--scope', scope],
                        [binary, 'plugin', 'install', f'repo-graph@{MARKETPLACE}', '--scope', scope]]
        plans.append(dict(harness=name, commands=commands, status='planned'))
    if not dry_run:
        completed = []
        for plan in plans:
            for step, command in enumerate(plan['commands'], 1):
                try:
                    subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
                except (OSError, subprocess.SubprocessError) as error:
                    prior = ', '.join(completed) or 'none'
                    detail = getattr(error, 'stderr', None) or getattr(error, 'stdout', None) or str(error)
                    if isinstance(detail, bytes):
                        detail = detail.decode('utf-8', errors='replace')
                    code = f'exit {error.returncode}' if isinstance(error, subprocess.CalledProcessError) else type(error).__name__
                    raise RuntimeError(f"{plan['harness']} native installation failed at step {step}/{len(plan['commands'])} ({code}); completed harnesses: {prior}. Native changes may remain; retry the same init command.\n{detail.strip()[-2000:]}") from None
            plan['status'] = 'installed'
            completed.append(plan['harness'])
    return dict(scope=scope, source=str(local) if local else REPOSITORY,
                ref=None if local else revision, dry_run=dry_run, harnesses=plans)
