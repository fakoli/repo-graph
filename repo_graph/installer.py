"""Initialize the canonical product through each harness's native manager."""
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import tempfile

from . import __version__

HARNESSES = ('pi', 'codex', 'claude')
MARKETPLACE = 'repo-graph'
REPOSITORY = 'fakoli/repo-graph'
MANIFESTS = {'pi': 'package.json', 'codex': '.agents/plugins/marketplace.json',
             'claude': '.claude-plugin/marketplace.json'}
PLUGIN_MANIFESTS = {'codex': '.codex-plugin/plugin.json', 'claude': '.claude-plugin/plugin.json'}
PLUGIN_ID = f'repo-graph@{MARKETPLACE}'
CORE_FILES = ('scripts/repo_graph.py', 'skills/repo-graph/SKILL.md', 'repo_graph/__init__.py')


def _local_product(root, name):
    file = MANIFESTS['pi'] if name == 'pi' else PLUGIN_MANIFESTS[name]
    expected_name = 'repo-graph-agent' if name == 'pi' else 'repo-graph'
    try:
        manifest = json.loads((root / file).read_text())
        valid = manifest.get('name') == expected_name and isinstance(manifest.get('version'), str) and bool(manifest['version'].strip())
        valid = valid and all((root / core).is_file() for core in CORE_FILES)
    except (OSError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError(f'Local product needs {file} with name {expected_name}, a version, and its runtime/script/skill files')


def _native(command):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)


def _claude_installed(binary, scope):
    plugins = json.loads(_native([binary, 'plugin', 'list', '--json']).stdout)
    return [plugin for plugin in plugins if plugin['id'] == PLUGIN_ID and plugin['scope'] == scope
            and (scope != 'project' or Path(plugin['projectPath']).resolve() == Path.cwd())]


def _marketplace(binary, name):
    marketplaces = json.loads(_native([binary, 'plugin', 'marketplace', 'list', '--json']).stdout)
    if name == 'codex':
        marketplaces = marketplaces['marketplaces']
    selected = [marketplace for marketplace in marketplaces if marketplace['name'] == MARKETPLACE]
    if len(selected) > 1:
        raise ValueError('Expected one registered repo-graph marketplace')
    return selected[0] if selected else None


def _source(marketplace, name):
    source_root = Path(marketplace['root'] if name == 'codex' else marketplace['installLocation']).resolve()
    registered = marketplace['marketplaceSource'] if name == 'codex' else marketplace
    source_type = registered['sourceType'] if name == 'codex' else registered['source']
    source_value = registered['source'] if name == 'codex' else registered.get('path', registered.get('url'))
    return source_root, registered, source_type, source_value


def _owned_marketplace(marketplace, name):
    if marketplace is None:
        return
    root, _, kind, value = _source(marketplace, name)
    if kind == 'git':
        owned = value == f'https://github.com/{REPOSITORY}.git'
    elif kind in {'local', 'directory'}:
        _local_product(root, name)
        owned = True
    else:
        owned = False
    if not owned:
        raise ValueError('The repo-graph marketplace name belongs to another source; resolve it with the native manager first')


def _prevalidate_remote(revision):
    # Check the replacement before removing a working native registration.
    with tempfile.TemporaryDirectory(prefix='repo-graph-init-') as scratch:
        _native(['git', 'init', scratch])
        _native(['git', '-C', scratch, 'fetch', '--depth', '1', f'https://github.com/{REPOSITORY}.git', revision])
        manifest = json.loads(_native(['git', '-C', scratch, 'show', f'FETCH_HEAD:{MANIFESTS["codex"]}']).stdout)
        plugin = json.loads(_native(['git', '-C', scratch, 'show', f'FETCH_HEAD:{PLUGIN_MANIFESTS["codex"]}']).stdout)
        if manifest.get('name') != MARKETPLACE or plugin.get('name') != 'repo-graph' or not plugin.get('version'):
            raise ValueError('Selected remote ref is missing the canonical product manifests')
        for file in CORE_FILES:
            _native(['git', '-C', scratch, 'cat-file', '-e', f'FETCH_HEAD:{file}'])


def _verify(binary, name, local, revision, scope, added):
    """Check native registration and the cached product before claiming success."""
    marketplace = _marketplace(binary, name)
    if marketplace is None:
        raise ValueError('Expected one registered repo-graph marketplace')
    source_root, registered, source_type, source_value = _source(marketplace, name)
    if local:
        if source_type not in {'local', 'directory'} or Path(source_value).resolve() != local or source_root != local:
            raise ValueError('Registered marketplace does not match the selected local source')
    else:
        if source_type != 'git' or source_value != f'https://github.com/{REPOSITORY}.git':
            raise ValueError('Registered marketplace does not match the selected remote source')
        if name == 'claude':
            if registered.get('ref') != revision:
                raise ValueError('Registered marketplace does not match the selected ref')
        else:
            head = _native(['git', '-C', str(source_root), 'rev-parse', 'HEAD']).stdout.strip()
            selected_ref = _native(['git', '-C', str(source_root), 'rev-parse', f'{revision}^{{commit}}']).stdout.strip()
            if head != selected_ref:
                raise ValueError('Marketplace Git HEAD does not match the selected ref')
    manifest = PLUGIN_MANIFESTS[name]
    expected_version = json.loads((source_root / manifest).read_text())['version']
    if name == 'claude':
        installed = _claude_installed(binary, scope)
        cached = Path(installed[0]['installPath']) if len(installed) == 1 else None
    else:
        plugins = json.loads(_native([binary, 'plugin', 'list', '--json']).stdout)
        installed = [plugin for plugin in plugins['installed'] if plugin['pluginId'] == PLUGIN_ID]
        cached = Path(added['installedPath'])
    if len(installed) != 1 or not installed[0]['enabled'] or installed[0]['version'] != expected_version:
        raise ValueError(f'Native installed plugin is not enabled at selected version {expected_version}')
    if json.loads((cached / manifest).read_text())['version'] != expected_version:
        raise ValueError(f'Cached plugin manifest does not match selected version {expected_version}')
    # These small, fixed product areas are the code and skill that will execute.
    files = [manifest, 'scripts/repo_graph.py', 'skills/repo-graph/SKILL.md']
    files.extend(str(path.relative_to(source_root)) for path in (source_root / 'repo_graph').rglob('*')
                 if path.is_file() and (path.suffix == '.py' or 'assets' in path.relative_to(source_root).parts))
    for file in sorted(set(files)):
        if hashlib.sha256((source_root / file).read_bytes()).digest() != hashlib.sha256((cached / file).read_bytes()).digest():
            raise ValueError(f'Cached product differs from selected source: {file}')
    return dict(version=expected_version, installed_path=str(cached), source=str(local) if local else REPOSITORY,
                ref=None if local else revision)


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
            _local_product(local, name)
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
                        [binary, 'plugin', 'add', PLUGIN_ID, '--json']]
            if not local:
                commands[0].extend(['--ref', revision])
        else:
            commands = [[binary, 'plugin', 'marketplace', 'add',
                         str(local) if local else f'https://github.com/{REPOSITORY}.git#{revision}', '--scope', scope],
                        [binary, 'plugin', 'install', PLUGIN_ID, '--scope', scope]]
        plans.append(dict(harness=name, commands=commands, status='planned'))
    if not dry_run:
        completed = []
        for plan in plans:
            name = plan['harness']
            stage = 'inventory'
            try:
                existing_marketplace = _marketplace(binaries[name], name) if name != 'pi' else None
                if name != 'pi':
                    _owned_marketplace(existing_marketplace, name)
                if name == 'claude' and _claude_installed(binaries[name], scope):
                    plan['commands'][1][2] = 'update'
                added = None
                for step, command in enumerate(list(plan['commands']), 1):
                    stage = f'step {step}/{len(plan["commands"])}'
                    try:
                        result = _native(command)
                    except subprocess.CalledProcessError as error:
                        if name != 'codex' or step != 1 or existing_marketplace is None or 'already added from a different source' not in (error.stderr or ''):
                            raise
                        stage = 'replacement preflight'
                        if local is None:
                            _prevalidate_remote(revision)
                        remove = [binaries[name], 'plugin', 'marketplace', 'remove', MARKETPLACE]
                        stage = 'marketplace replacement'
                        _native(remove)
                        plan['commands'].insert(0, remove)
                        result = _native(command)
                    if name == 'codex' and step == 2:
                        added = json.loads(result.stdout)
                if name != 'pi':
                    stage = 'verification'
                    plan['verification'] = _verify(binaries[name], name, local, revision, scope, added)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
                prior = ', '.join(completed) or 'none'
                detail = getattr(error, 'stderr', None) or getattr(error, 'stdout', None) or str(error)
                if isinstance(detail, bytes):
                    detail = detail.decode('utf-8', errors='replace')
                code = f'exit {error.returncode}' if isinstance(error, subprocess.CalledProcessError) else type(error).__name__
                raise RuntimeError(f"{name} native installation failed at {stage} ({code}); completed harnesses: {prior}. Native changes may remain; retry the same init command.\n{detail.strip()[-2000:]}") from None
            plan['status'] = 'installed'
            completed.append(plan['harness'])
    return dict(scope=scope, source=str(local) if local else REPOSITORY,
                ref=None if local else revision, dry_run=dry_run, harnesses=plans)
