"""Experimental, source-only syntax/direct-binding baseline; never reads gold.

Optional native wheels are confined to the analysis extra. Finite framework
registrations are opt-in; types, points-to analysis and runtime calls are unmodeled.
"""
from dataclasses import dataclass, field
import ast
import copy
import hashlib
import importlib
from importlib import metadata
import json
import math
from pathlib import Path, PurePosixPath
from xml.parsers import expat
import posixpath
import re
import time

from repo_graph.source import SourceRoot, source_hash

PINS = {
    'tree-sitter': '0.26.0', 'tree-sitter-python': '0.25.0',
    'tree-sitter-go': '0.25.0', 'tree-sitter-javascript': '0.25.0',
    'tree-sitter-typescript': '0.23.2',
}
LANGUAGES = ('python', 'go', 'javascript', 'typescript')
_EXPRESSION_SPELLINGS = ('identifier', 'string', 'interpreted_string_literal', 'raw_string_literal')
RULE_VERSION = 'syntax-direct-v1'
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# Filename constraints are classified against a fixed Go source revision, not
# this host's build environment. Ordinary underscores are not build tags.
# https://github.com/golang/go/blob/3901409b5d0fb7c85a3e6730a59943cc93b2835c/src/go/build/build.go
# https://github.com/golang/go/blob/3901409b5d0fb7c85a3e6730a59943cc93b2835c/src/internal/syslist/syslist.go
GO_FILENAME_POLICY = {
    'version': 'go1.24.0-build-neutral-v1',
    'source_revision': '3901409b5d0fb7c85a3e6730a59943cc93b2835c',
    'build_sha256': 'e2c9d4056a147bce2b170cce9c9dff26ca9ef9ca8aa0af85c2ead342a3277bb8',
    'syslist_sha256': '079a54068737d10e87aae714c07b6074e78b8c020edd696953ae01d3086a3010',
    'platform_selection': 'unqualified; no GOOS, GOARCH, tags or active toolchain supplied',
    'unknown_suffix': 'ordinary filename under pinned KnownOS/KnownArch tables; no future-platform inference',
}
GO_KNOWN_OS = frozenset(('aix', 'android', 'darwin', 'dragonfly', 'freebsd', 'hurd',
    'illumos', 'ios', 'js', 'linux', 'nacl', 'netbsd', 'openbsd', 'plan9', 'solaris',
    'wasip1', 'windows', 'zos'))
GO_KNOWN_ARCH = frozenset(('386', 'amd64', 'amd64p32', 'arm', 'armbe', 'arm64',
    'arm64be', 'loong64', 'mips', 'mipsle', 'mips64', 'mips64le', 'mips64p32',
    'mips64p32le', 'ppc', 'ppc64', 'ppc64le', 'riscv', 'riscv64', 's390', 's390x',
    'sparc', 'sparc64', 'wasm'))


def go_filename_class(path):
    """Shared finite membership policy; classify constraints, never select them."""
    name = PurePosixPath(path).name
    if name.startswith(('_', '.')):
        return 'ignored_filename'
    if not name.endswith('.go'):
        return 'not_go_source'
    if name.endswith('_test.go'):
        return 'test_file'
    # Go cuts at the first dot, then requires a nonempty prefix before '_'.
    stem = name.split('.', 1)[0]
    if '_' in stem:
        suffix = stem.rsplit('_', 1)[1]
        if suffix in GO_KNOWN_OS or suffix in GO_KNOWN_ARCH:
            return 'platform_variant'
    return 'neutral'


def snapshot_path(repository_id, revision, physical_path):
    """Internal namespace only; emitted paths remain original physical paths."""
    if (not isinstance(repository_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9._-]{0,95}', repository_id) or
            not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision)):
        raise ValueError('Invalid snapshot identity')
    if (not isinstance(physical_path, str) or len(physical_path) > 4096 or '\0' in physical_path or '\\' in physical_path or
            PurePosixPath(physical_path).as_posix() != physical_path):
        raise ValueError('Physical source path must be canonical')
    SourceRoot.parts(physical_path)
    namespace = hashlib.sha256((repository_id + '\0' + revision).encode()).hexdigest()
    return 'snapshots/' + namespace + '/' + physical_path


def go_module(raw, work):
    """Finite unquoted declarations; record runtime/replacement uncertainty."""
    module, requirements, replacements, runtime_controls = None, {}, [], {}
    block, seen, replacement_keys = None, set(), set()
    path_pattern = r'[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}'
    version_pattern = r'v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)?(?:\+[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)?'
    def module_path(value):
        return bool(re.fullmatch(path_pattern, value) and
                    all(part not in ('', '.', '..') for part in value.split('/')))
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeError:
        return None
    for line in lines:
        work.check()
        tokens = line.split('//', 1)[0].strip().split()
        if not tokens:
            continue
        if block and tokens == [')']:
            block = None
            continue
        if block:
            directive, entry = block, tokens
        else:
            directive, entry = tokens[0], tokens[1:]
            if directive in ('require', 'replace', 'godebug') and entry == ['(']:
                block = directive
                continue
        if directive == 'module':
            if len(entry) != 1 or module is not None or not module_path(entry[0]):
                return None
            module = entry[0]
        elif directive in ('go', 'toolchain'):
            pattern = r'[1-9]\d*\.\d+(?:\.\d+)?' if directive == 'go' else r'(?:default|go[1-9]\d*\.\d+(?:\.\d+)?)'
            if directive in seen or len(entry) != 1 or not re.fullmatch(pattern, entry[0]):
                return None
            seen.add(directive)
        elif directive == 'require':
            if (len(entry) != 2 or not module_path(entry[0]) or
                    not re.fullmatch(version_pattern, entry[1]) or entry[0] in requirements):
                return None
            requirements[entry[0]] = entry[1]
        elif directive == 'godebug':
            if len(entry) != 1 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}=[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', entry[0]):
                return None
            key, value = entry[0].split('=', 1)
            if key in runtime_controls:
                return None
            runtime_controls[key] = value
        elif directive == 'replace':
            if entry.count('=>') != 1:
                return None
            separator = entry.index('=>')
            old, new = entry[:separator], entry[separator + 1:]
            if (len(old) not in (1, 2) or len(new) not in (1, 2) or
                    not module_path(old[0]) or (len(old) == 2 and not re.fullmatch(version_pattern, old[1]))):
                return None
            if len(new) == 2:
                if not module_path(new[0]) or not re.fullmatch(version_pattern, new[1]):
                    return None
            elif not re.fullmatch(r'(?:\.{1,2}(?:/[A-Za-z0-9._/-]+)?|/[A-Za-z0-9._/-]+)', new[0]):
                return None
            key = old[0], old[1] if len(old) == 2 else None
            if key in replacement_keys:
                return None
            replacement_keys.add(key)
            replacements.append({'module': old[0], 'version': key[1],
                                 'replacement': new[0], 'replacement_version': new[1] if len(new) == 2 else None})
        else:
            return None
    if block or module is None:
        return None
    return {'module': module, 'requirements': requirements,
            'replacements': replacements, 'runtime_controls': runtime_controls}


def go_contexts(files, configurations, context, work):
    """Registration bounds domains; raw declarations choose packages and symbols."""
    if not isinstance(context, dict) or set(context) != {'policy', 'dependencies', 'packages', 'controls'} or context['policy'] != 'declared_snapshot_only':
        raise ValueError('Explicit declared-snapshot source context required')
    if any(not isinstance(context[key], list) or len(context[key]) > work.budget.max_files
           for key in ('dependencies', 'packages', 'controls')):
        raise ValueError('Source context inventory exceeds bound')
    modules, packages, dependencies = {}, {}, []
    manifest_files = 0
    for control in context['controls']:
        work.check()
        if (not isinstance(control, dict) or set(control) not in (
                {'repository_id', 'revision', 'qualified'},
                {'repository_id', 'revision', 'qualified', 'namespace_qualified'}) or
                type(control['qualified']) is not bool or
                ('namespace_qualified' in control and type(control['namespace_qualified']) is not bool)):
            raise ValueError('Invalid source-control metadata')
        owner = control['repository_id'], control['revision']
        if owner in modules:
            raise ValueError('Duplicate source-control identity')
        config = configurations.get(snapshot_path(*owner, 'go.mod'))
        namespace = control.get('namespace_qualified', control['qualified'])
        parsed = go_module(config, work) if config is not None and namespace else None
        # Legacy controls retain their old refusal of previously unknown syntax.
        if parsed and 'namespace_qualified' not in control and (parsed['replacements'] or parsed['runtime_controls']):
            parsed = None
        if parsed:
            parsed = dict(parsed, external_qualified=bool(control['qualified'] and not parsed['replacements']))
        modules[owner] = parsed
    owners = snapshot_owners(list(files) + list(configurations), modules, work)
    for manifest in context['packages']:
        work.check()
        if not isinstance(manifest, dict) or set(manifest) != {'repository_id', 'revision', 'directory', 'files'}:
            raise ValueError('Invalid package inventory metadata')
        owner = manifest['repository_id'], manifest['revision']
        directory = manifest['directory']
        if not isinstance(directory, str) or len(directory) > 4096:
            raise ValueError('Invalid package directory')
        snapshot_path(*owner, posixpath.join(directory, 'sentinel.go'))
        if directory != posixpath.dirname(posixpath.join(directory, 'sentinel.go')):
            raise ValueError('Package directory must be canonical')
        key = owner, directory
        if key in packages or not isinstance(manifest['files'], list) or len(manifest['files']) > work.budget.max_files:
            raise ValueError('Duplicate or oversized package inventory')
        manifest_files += len(manifest['files'])
        if manifest_files > work.budget.max_files:
            raise ValueError('Combined package inventory exceeds file bound')
        expected = {}
        for item in manifest['files']:
            if (not isinstance(item, dict) or set(item) != {'path', 'sha256', 'bytes'} or
                    not isinstance(item['path'], str) or item['path'] in expected or
                    not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']) or
                    type(item['bytes']) is not int or not 0 <= item['bytes'] <= work.budget.max_file_bytes):
                raise ValueError('Invalid or duplicate package file metadata')
            snapshot_path(*owner, item['path'])
            if (posixpath.dirname(item['path']) != directory or
                    go_filename_class(item['path']) not in ('neutral', 'platform_variant')):
                raise ValueError('Package inventory must name eligible non-test Go files')
            expected[item['path']] = item
        actual = [file for file in files.values() if file.language == 'go' and
                  (owners[file.path]['repository_id'], owners[file.path]['revision']) == owner and
                  posixpath.dirname(owners[file.path]['physical_path']) == directory]
        valid = bool(expected) and {owners[file.path]['physical_path'] for file in actual} == set(expected)
        names, uncertainty = set(), set()
        for file in actual:
            work.check()
            physical = owners[file.path]['physical_path']
            metadata = expected.get(physical, {})
            clauses = file.syntax_metadata['package_clauses']
            name = clauses[0]['name'] if len(clauses) == 1 else ''
            names.add(name)
            # Recognized filename constraints and all compiler/build directives
            # stay unknown. No platform, tag or CGO selection is inferred.
            variant = go_filename_class(physical) == 'platform_variant'
            if variant:
                uncertainty.add('GOOS/GOARCH filename selection is unqualified')
            if file.syntax_metadata['go_cgo_import']:
                variant = True
                uncertainty.add('CGO selection is unqualified')
            if file.syntax_metadata['go_bodyless_function']:
                variant = True
                uncertainty.add('bodyless Go declaration is unqualified')
            if file.syntax_metadata['go_control_directive']:
                variant = True
                uncertainty.add('Go build/compiler directive is unqualified')
            valid = valid and not file.partial and not variant and bool(name) and (
                metadata.get('sha256') == file.record['sha256'] and metadata.get('bytes') == file.record['bytes'])
        packages[key] = {'paths': [file.path for file in actual], 'name': next(iter(names)) if len(names) == 1 else None,
                         'qualified': bool(valid and len(names) == 1), 'uncertainty': sorted(uncertainty)}
    required = {'consumer_repository_id', 'consumer_revision', 'dependency_repository_id',
                'dependency_revision', 'module_path', 'version'}
    identities = set()
    for entry in context['dependencies']:
        work.check()
        if (not isinstance(entry, dict) or set(entry) != required or
                not isinstance(entry['module_path'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}', entry['module_path']) or
                any(part in ('', '.', '..') for part in entry['module_path'].split('/')) or
                not isinstance(entry['version'], str) or len(entry['version']) > 128):
            raise ValueError('Dependency registration must contain source identity metadata only')
        consumer = entry['consumer_repository_id'], entry['consumer_revision']
        provider = entry['dependency_repository_id'], entry['dependency_revision']
        snapshot_path(*consumer, 'go.mod')
        snapshot_path(*provider, 'go.mod')
        identity = consumer, entry['module_path'], entry['version'], provider
        if identity in identities:
            raise ValueError('Duplicate dependency registration')
        identities.add(identity)
        version = re.fullmatch(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*))?', entry['version'])
        prerelease = version[4] if version else None
        ordinary_version = bool(version and not re.search(r'[.-]\d{14}-[0-9a-f]{12}$', entry['version']) and
                                not any(part.isdecimal() and len(part) > 1 and part.startswith('0')
                                        for part in (prerelease or '').split('.')))
        major = int(version[1]) if version else -1
        path_major = re.search(r'/v(\d+)$', entry['module_path'])
        canonical_major = (major >= 2 and path_major and int(path_major[1]) == major) or (major in (0, 1) and path_major is None)
        caller, dependency = modules.get(consumer), modules.get(provider)
        qualified = bool(ordinary_version and canonical_major and caller and caller['external_qualified'] and dependency and
                         caller['requirements'].get(entry['module_path']) == entry['version'] and
                         dependency['module'] == entry['module_path'])
        dependencies.append(dict(entry, consumer=consumer, provider=provider, qualified=qualified))
    return {'modules': modules, 'packages': packages, 'dependencies': dependencies, 'owners': owners}


def snapshot_blobs(blobs, context, work):
    """Validate the complete bounded namespace before collecting any source fact."""
    if not isinstance(blobs, list) or len(blobs) > work.budget.max_files:
        raise ValueError('Snapshot inventory exceeds file bound')
    identities, paths, total = set(), set(), 0
    required = {'path', 'physical_path', 'repository_id', 'revision', 'language', 'content', 'kind', 'sha256', 'bytes'}
    for blob in blobs:
        work.check()
        if not isinstance(blob, dict) or set(blob) != required:
            raise ValueError('Snapshot blobs require source metadata only')
        expected = snapshot_path(blob['repository_id'], blob['revision'], blob['physical_path'])
        identity = blob['repository_id'], blob['revision'], blob['physical_path']
        if blob['path'] != expected or expected in paths or identity in identities:
            raise ValueError('Noncanonical or duplicate snapshot identity')
        paths.add(expected)
        identities.add(identity)
        raw = blob['content']
        if (not isinstance(raw, bytes) or len(raw) > work.budget.max_file_bytes or
                type(blob['bytes']) is not int or blob['bytes'] != len(raw) or
                blob['sha256'] != hashlib.sha256(raw).hexdigest() or
                blob['kind'] not in ('source', 'configuration') or blob['language'] not in (*LANGUAGES, 'unknown')):
            raise ValueError('Snapshot full source identity or input bound mismatch')
        total += len(raw)
        if total > work.budget.max_total_bytes:
            raise ValueError('Snapshot inventory exceeds byte bound')
    # Validate all registrations/manifests and duplicate identities before parse.
    validated = go_contexts({}, {}, context, work)
    return snapshot_owners(paths, validated['modules'], work)



def snapshot_owners(paths, modules, work):
    """Bind source namespaces to declared controls without extending compact records."""
    prefixes, owners = {}, {}
    for owner in modules:
        work.check()
        prefix = snapshot_path(*owner, 'sentinel').rsplit('/', 1)[0] + '/'
        if prefix in prefixes:
            raise ValueError('Duplicate snapshot namespace')
        prefixes[prefix] = owner
    for path in paths:
        work.check()
        matches = [(prefix, owner) for prefix, owner in prefixes.items() if path.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError('Source path outside registered snapshot namespace')
        prefix, owner = matches[0]
        physical = path[len(prefix):]
        if snapshot_path(*owner, physical) != path:
            raise ValueError('Noncanonical snapshot source identity')
        owners[path] = dict(repository_id=owner[0], revision=owner[1], physical_path=physical)
    return owners


def project_snapshot(facts, inventory, errors, owners):
    """Project copies after binding; collected JSON stays target-free and reusable."""
    facts, inventory, errors = copy.deepcopy(facts), copy.deepcopy(inventory), copy.deepcopy(errors)
    for fact in facts['definitions'] + facts['sites']:
        owner = owners[fact['path']]
        fact.update(path=owner['physical_path'], repository_id=owner['repository_id'], revision=owner['revision'])
        fact['provenance'].update(owner)
    for item in inventory + errors:
        owner = owners[item['path']]
        item.update(owner, path=owner['physical_path'])
    return facts, inventory, errors


class BackendUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Budget:
    max_files: int = 128
    max_file_bytes: int = 512 * 1024
    max_total_bytes: int = 4 * 1024 * 1024
    max_nodes: int = 200_000
    max_facts: int = 40_000
    timeout_seconds: float = 30.0
    max_collected_bytes: int = 64 * 1024 * 1024
    max_handoff_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        integers = (self.max_files, self.max_file_bytes, self.max_total_bytes,
                    self.max_nodes, self.max_facts, self.max_collected_bytes, self.max_handoff_bytes)
        if (any(type(value) is not int or value <= 0 for value in integers) or
                type(self.timeout_seconds) not in (int, float) or
                not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError('Budgets must be positive')


class StopScan(RuntimeError):
    pass


class Work:
    def __init__(self, budget, cancel):
        self.budget, self.cancel = budget, cancel
        self.started, self.nodes, self.facts = time.perf_counter(), 0, 0
        self.collected_bytes = 0
        self.text_bytes = 0
        self.parse_seconds = 0.0

    def check(self):
        if self.cancel is not None and self.cancel():
            raise StopScan('cancelled')
        if time.perf_counter() - self.started > self.budget.timeout_seconds:
            raise StopScan('deadline_exceeded')

    def node(self):
        self.nodes += 1
        self.check()
        if self.nodes > self.budget.max_nodes:
            raise StopScan('node_budget_exceeded')

    def fact(self):
        self.facts += 1
        self.check()
        if self.facts > self.budget.max_facts:
            raise StopScan('fact_budget_exceeded')

    def retain(self, size):
        self.collected_bytes += size
        self.check()
        if self.collected_bytes > self.budget.max_collected_bytes:
            raise StopScan('collected_byte_budget_exceeded')

    def text(self, size):
        # Check before copying overlapping class/function bodies or site text.
        self.text_bytes += size
        self.check()
        if self.text_bytes > self.budget.max_collected_bytes:
            raise StopScan('collected_byte_budget_exceeded')


def backend():
    """Reject missing/unqualified versions; never installs or downloads code."""
    try:
        versions = {name: metadata.version(name) for name in PINS}
        if versions != PINS:
            raise BackendUnavailable('Optional backend versions differ from qualified pins')
        ts = importlib.import_module('tree_sitter')
        parsers = {}
        for language in LANGUAGES:
            grammar = importlib.import_module('tree_sitter_' + language)
            entry = 'language_typescript' if language == 'typescript' else 'language'
            parsers[language] = ts.Parser(ts.Language(getattr(grammar, entry)()))
        return parsers, versions
    except (ImportError, metadata.PackageNotFoundError) as error:
        raise BackendUnavailable('Missing analysis backend; run uv sync --extra analysis') from error


def span(raw, node, end=None):
    start, end = node.start_byte, node.end_byte if end is None else end
    # The pinned 0.26.0 Point.row/column C getters return borrowed references;
    # accessing large integers through those getters corrupts the heap. Point
    # implements the tuple interface, whose indexed/iterated values own refs.
    # Primary source: py-tree-sitter/v0.26.0/tree_sitter/binding/point.c.
    row, column = node.end_point
    if end > node.end_byte:
        extension = raw[node.end_byte:end]
        row += extension.count(b'\n')
        column = len(extension) - extension.rfind(b'\n') - 1 if b'\n' in extension else column + len(extension)
    return {'start_byte': start, 'end_byte': end,
            'start_line': node.start_point[0] + 1,
            'end_line': row + 1 - int(column == 0 and end > start)}


def text(raw, node):
    return raw[node.start_byte:node.end_byte].decode('utf-8') if node else ''


def unwrap(node):
    if node is not None and node.type in ('expression_list', 'parenthesized_expression'):
        return node.named_children[0] if len(node.named_children) == 1 else node
    return node


def identifiers(node):
    """Binding patterns only; callers must not pass type annotations."""
    if node is None:
        return []
    if node.type in ('identifier', 'shorthand_property_identifier_pattern'):
        return [node]
    result = []
    for child in node.named_children:
        if child.type not in ('type_annotation', 'type_identifier', 'function_type', 'predefined_type'):
            result.extend(identifiers(child))
    return result


@dataclass
class Binding:
    kind: str
    value: object
    node: object
    scope: object
    hoisted: bool = False


@dataclass
class Scope:
    parent: object
    kind: str
    name: str
    owner: object = None
    bindings: dict = field(default_factory=dict)
    ordinal: int = 0
    range: object = None

    def add(self, name, binding):
        self.bindings.setdefault(name, []).append(binding)

    def lookup(self, name):
        scope = self
        while scope:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return []


@dataclass(frozen=True)
class Position:
    start_byte: int


@dataclass(frozen=True)
class Expression:
    """Only syntax consulted by the v1 resolver; never a native Node."""
    type: str
    start_byte: int
    end_byte: int
    spelling: str = ''
    base_identifier: bool = False
    base: str = ''
    member: str = ''

    @classmethod
    def lower(cls, raw, node):
        if node is None:
            return None
        base = member = None
        if node.type in ('attribute', 'member_expression', 'selector_expression'):
            base = node.child_by_field_name('object') or node.child_by_field_name('operand')
            member = node.child_by_field_name('attribute') or node.child_by_field_name('property') or node.child_by_field_name('field')
        direct = base is not None and base.type == 'identifier'
        return cls(node.type, node.start_byte, node.end_byte,
                   text(raw, node) if node.type in _EXPRESSION_SPELLINGS else '', direct,
                   text(raw, base) if direct else '', text(raw, member) if direct else '')

    def payload(self):
        return dict(type=self.type, start_byte=self.start_byte, end_byte=self.end_byte,
                    spelling=self.spelling, base_identifier=self.base_identifier,
                    base=self.base, member=self.member)


def collector_identity():
    """Pin actual collector/resolver code as well as its declared rule version."""
    observed = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if observed != _LOADED_SOURCE_SHA256:
        raise ValueError('Collector implementation changed since module import')
    return observed


def _json_bytes(payload, budget, cancel=None, *, measure=False):
    """Bound encoding before assembling a handoff; no pickle or code objects."""
    chunks, size, started = None if measure else [], 0, time.perf_counter()
    for chunk in json.JSONEncoder(ensure_ascii=True, separators=(',', ':')).iterencode(payload):
        if cancel is not None and cancel():
            raise StopScan('cancelled')
        if time.perf_counter() - started > budget.timeout_seconds:
            raise StopScan('deadline_exceeded')
        part = chunk.encode('utf-8')
        size += len(part)
        if size > budget.max_handoff_bytes:
            raise StopScan('handoff_byte_budget_exceeded')
        if chunks is not None:
            chunks.append(part)
    return size if measure else b''.join(chunks)


@dataclass
class CollectedFile:
    """Node-free per-file handoff shared by serial and future finite workers.

    Legacy full definition/site text is preserved, so body text can overlap.
    Aggregate JSON-equivalent retention is capped by max_collected_bytes;
    each handoff is capped by max_handoff_bytes. Neither is a native RSS bound.
    No source blob, Tree, Node, parser, callback or Work is retained here.
    """
    record: dict
    scopes: list
    definitions: list
    candidates: list
    imports: list
    errors: list
    partial: bool
    counts: dict
    syntax_metadata: dict
    collector_sha256: str = field(default_factory=collector_identity)
    sites: list = field(default_factory=list)

    @property
    def path(self):
        return self.record['path']

    @property
    def language(self):
        return self.record['language']

    @property
    def module(self):
        return self.scopes[0]

    @property
    def collected_fact_count(self):
        syntax = self.syntax_metadata
        return (len(self.definitions) + len(self.imports)
                + len(syntax['import_contexts'])
                + sum(1 + len(row['arguments']) + sum(len(argument['container_references']) + len(argument['elements'])
                          for argument in row['arguments']) for row in syntax['calls'])
                + sum(1 + len(row['bases']) + 3 * len(row['decorators']) + bool(row['header']) + bool(row['decorator_stack']) for row in syntax['python_declarations'])
                + sum(3 + len(row['elements']) + len(row['container_references'])
                      for row in syntax['python_assignments'])
                + sum(1 + sum(3 + len(pair['elements']) for pair in row['pairs']) for row in syntax['python_dictionaries']))

    def payload(self):
        if self.collector_sha256 != collector_identity():
            raise ValueError('Collected facts were produced by stale implementation')
        scopes = []
        for scope in self.scopes:
            bindings = {}
            for name, entries in scope.bindings.items():
                bindings[name] = [dict(kind=b.kind,
                    value=b.value.payload() if b.kind == 'alias' else b.value,
                    start_byte=b.node.start_byte, scope=b.scope.ordinal, hoisted=b.hoisted)
                    for b in entries]
            scopes.append(dict(id=scope.ordinal, parent=scope.parent.ordinal if scope.parent else None,
                               kind=scope.kind, name=scope.name, owner=scope.owner, bindings=bindings, range=scope.range))
        return {'schema_version': 1, 'rules': RULE_VERSION, 'collector_sha256': self.collector_sha256,
                'versions': dict(PINS), 'record': self.record, 'scopes': scopes,
                'definitions': self.definitions,
                'candidates': [dict(scope=scope.ordinal, callee=expr.payload() if expr else None, fact=fact)
                               for fact, expr, scope in self.candidates],
                'imports': self.imports, 'errors': self.errors, 'partial': self.partial, 'counts': self.counts,
                'syntax_metadata': self.syntax_metadata}

    def to_json(self, budget=None, cancel=None):
        return _json_bytes(self.payload(), budget or Budget(), cancel)

    @classmethod
    def from_json(cls, encoded, expected_record, expected_sha256, budget=None, cancel=None):
        """Decode only a bounded handoff pinned by a trusted collector receipt.

        The expected digest must come from the owned producer, not this payload.
        It provides integrity, not authentication of an arbitrary worker. Source
        ownership/freshness and typed links are checked independently below.
        No final cross-file targets are accepted in this collected form.
        """
        budget = budget or Budget()
        work = Work(budget, cancel)
        work.check()
        if type(encoded) is not bytes or len(encoded) > min(budget.max_handoff_bytes, budget.max_collected_bytes):
            raise ValueError('Invalid or unbounded compact handoff')
        if not _sha(expected_sha256) or hashlib.sha256(encoded).hexdigest() != expected_sha256:
            raise ValueError('Compact handoff differs from trusted producer digest')
        def pairs(items):
            result = {}
            for name, value in items:
                if name in result:
                    raise ValueError('Duplicate compact JSON key')
                result[name] = value
            return result
        def reject_number(value):
            raise ValueError('Compact JSON requires finite integer numbers')
        try:
            payload = json.loads(encoded, object_pairs_hook=pairs,
                                 parse_constant=reject_number, parse_float=reject_number)
        except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
            raise ValueError('Invalid compact JSON') from error
        work.check()
        try:
            return _decode_collected(payload, expected_record, work)
        except (UnicodeError, TypeError, KeyError, RecursionError) as error:
            raise ValueError('Invalid compact typed payload') from error

    def emit_sites(self, resolve, work):
        self.sites = []
        for fact, callee, scope in self.candidates:
            work.check()
            targets, reason, method = resolve(self, callee, scope, fact['range']['start_byte'])
            if fact['syntax_role'] == 'call_or_conversion':
                targets, reason, method = [], 'Go grammar cannot distinguish this call from a type conversion without type information', 'unsupported_go_ambiguity'
            if self.partial:
                targets, reason, method = [], 'file contains parse errors; bindings are withheld', 'partial_parse'
            work.fact()
            site = {key: fact[key] for key in ('id', 'path', 'language', 'role', 'range', 'text', 'caller')}
            site.update(targets=targets, certainty='resolved' if targets else 'unresolved',
                        targets_exhaustive=bool(targets), reason=reason, resolution_method=method,
                        syntax_role=fact['syntax_role'], provenance=fact['provenance'])
            if self.language == 'go' and getattr(resolve, 'declared_snapshot_scope', False):
                site['provenance'] = dict(fact['provenance'], binding_scope='declared_snapshot_only',
                                          active_build_qualified=False, runtime_qualified=False, mvs_qualified=False)
            elif self.language == 'go' and getattr(resolve, 'inventoried_package_scope', False):
                site['provenance'] = dict(fact['provenance'], binding_scope='inventoried_package_only',
                                          active_build_qualified=False, runtime_qualified=False, mvs_qualified=False)
            self.sites.append(site)
        if self.language == 'python' and getattr(resolve, 'framework_context', None) is not None:
            self.sites.extend(resolve.framework_sites(self, work))


def _sha(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _record_valid(record, budget):
    if type(record) is not dict or set(record) != {'path', 'language', 'bytes', 'sha256', 'kind'}:
        raise ValueError('Invalid compact source identity')
    path = record['path']
    if (type(path) is not str or len(path) > 4096 or '\\' in path or
            str(PurePosixPath(path)) != path or not path or path == '.'):
        raise ValueError('Noncanonical compact source path')
    try:
        SourceRoot.parts(path)
    except OSError as error:
        raise ValueError('Invalid compact source path') from error
    if (type(record['language']) is not str or record['language'] not in LANGUAGES or
            type(record['kind']) is not str or record['kind'] != 'source' or
            type(record['bytes']) is not int or not 0 <= record['bytes'] <= budget.max_file_bytes or
            not _sha(record['sha256'])):
        raise ValueError('Invalid compact source metadata')


def _decode_collected(payload, expected_record, work):
    """Validate every typed link before reconstructing node-free dataclasses."""
    budget = work.budget
    def shape(item, keys):
        work.check()
        if type(item) is not dict or set(item) != set(keys.split()):
            raise ValueError('Invalid compact object fields')
    def integer(value, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Invalid compact integer/range')
    def string(value):
        if type(value) is not str or len(value.encode('utf-8')) > budget.max_handoff_bytes:
            raise ValueError('Invalid compact string')
    def boolean(value):
        if type(value) is not bool:
            raise ValueError('Invalid compact boolean')
    def sequence(value, maximum):
        if type(value) is not list or len(value) > maximum:
            raise ValueError('Invalid or unbounded compact list')
    shape(payload, 'schema_version rules collector_sha256 versions record scopes definitions candidates imports errors partial counts syntax_metadata')
    _record_valid(expected_record, budget)
    _record_valid(payload['record'], budget)
    record = payload['record']
    if (type(payload['schema_version']) is not int or payload['schema_version'] != 1 or
            payload['rules'] != RULE_VERSION or payload['collector_sha256'] != collector_identity() or
            payload['versions'] != PINS or record != expected_record):
        raise ValueError('Stale or foreign compact handoff identity')
    size = record['bytes']
    def location(item):
        shape(item, 'start_byte end_byte start_line end_line')
        integer(item['start_byte'], 0, size)
        integer(item['end_byte'], item['start_byte'], size)
        integer(item['start_line'], 1, size + 1)
        integer(item['end_line'], item['start_line'], size + 1)
    def provenance(item):
        shape(item, 'source_sha256 rule_version syntax_kind evidence_kind')
        if (item['source_sha256'] != record['sha256'] or item['rule_version'] != RULE_VERSION or
                item['evidence_kind'] != 'static_syntax'):
            raise ValueError('Foreign compact fact provenance')
        string(item['syntax_kind'])
    def source_fact(item):
        if item['path'] != record['path'] or item['language'] != record['language']:
            raise ValueError('Foreign compact fact path/language')
        location(item['range'])
        provenance(item['provenance'])
        string(item['text'])
        if len(item['text'].encode('utf-8')) != item['range']['end_byte'] - item['range']['start_byte']:
            raise ValueError('Compact text differs from source range size')
    def expression(item):
        shape(item, 'type start_byte end_byte spelling base_identifier base member')
        for key in ('type', 'spelling', 'base', 'member'):
            string(item[key])
        integer(item['start_byte'], 0, size)
        integer(item['end_byte'], item['start_byte'], size)
        boolean(item['base_identifier'])
        is_member = item['type'] in ('attribute', 'member_expression', 'selector_expression')
        if (item['base_identifier'] and not is_member or
                not item['base_identifier'] and (item['base'] or item['member']) or
                item['type'] not in _EXPRESSION_SPELLINGS and item['spelling'] or
                item['type'] in _EXPRESSION_SPELLINGS and len(item['spelling'].encode('utf-8')) != item['end_byte'] - item['start_byte']):
            raise ValueError('Invalid compact callee descriptor')
        return Expression(**item)
    def container_references(items, start, end):
        sequence(items, budget.max_nodes)
        previous = start
        for item in items:
            value = expression(item)
            if value.type != 'identifier' or not previous <= value.start_byte < value.end_byte <= end:
                raise ValueError('Compact container reference outside its operand or source order')
            previous = value.end_byte
        if record['language'] != 'python' and items:
            raise ValueError('Foreign compact container references')
    sequence(payload['definitions'], budget.max_facts)
    definitions, by_id = payload['definitions'], {}
    for item in definitions:
        shape(item, 'id path language name kind range text provenance callable')
        source_fact(item)
        string(item['name'])
        if item['kind'] not in ('function', 'method', 'class', 'interface', 'type', 'method_declaration', 'function_value'):
            raise ValueError('Invalid compact definition kind')
        boolean(item['callable'])
        if item['callable'] != (item['kind'] in ('function', 'method', 'function_value', 'class')):
            raise ValueError('Invalid compact definition capability')
        expected = f'{record["path"]}:{item["range"]["start_byte"]}:{item["range"]["end_byte"]}'
        if item['id'] != expected or item['id'] in by_id:
            raise ValueError('Duplicate or forged compact definition ID')
        by_id[item['id']] = item
    def owner(value):
        if value is not None and (type(value) is not str or value not in by_id):
            raise ValueError('Foreign compact definition owner')
    def import_spec(item):
        shape(item, 'name module symbol range path language role text provenance explicit_alias' if record['language'] == 'go' else 'name module symbol range path language role text provenance')
        source_fact(item)
        if item['role'] != 'import':
            raise ValueError('Invalid compact import role')
        if record['language'] == 'go':
            boolean(item['explicit_alias'])
        string(item['name'])
        string(item['module'])
        if item['symbol'] is not None:
            string(item['symbol'])
        location(item['range'])
        return item['name'], item['module'], item['symbol'], item.get('explicit_alias'), tuple(item['range'][k] for k in ('start_byte', 'end_byte', 'start_line', 'end_line'))
    sequence(payload['imports'], budget.max_nodes)
    import_keys = {import_spec(item) for item in payload['imports']}
    sequence(payload['scopes'], budget.max_nodes + 1)
    if not payload['scopes']:
        raise ValueError('Compact module scope absent')
    scopes = []
    binding_count = 0
    for index, item in enumerate(payload['scopes']):
        shape(item, 'id parent kind name owner bindings range')
        location(item['range'])
        if type(item['id']) is not int or item['id'] != index:
            raise ValueError('Noncanonical compact scope ID')
        if index == 0:
            if item['parent'] is not None or item['kind'] != 'module' or item['name'] != '' or item['owner'] is not None:
                raise ValueError('Invalid compact module scope')
        else:
            integer(item['parent'], 0, index - 1)
            if item['kind'] not in ('function', 'class', 'block'):
                raise ValueError('Invalid compact scope kind')
        string(item['name'])
        owner(item['owner'])
        if type(item['bindings']) is not dict or len(item['bindings']) > budget.max_nodes:
            raise ValueError('Unbounded compact bindings')
        scope = Scope(scopes[item['parent']] if index else None, item['kind'], item['name'],
                      item['owner'], ordinal=index, range=item['range'])
        scopes.append(scope)
        for name, entries in item['bindings'].items():
            string(name)
            sequence(entries, budget.max_nodes)
            binding_count += len(entries)
            if not entries or binding_count > budget.max_nodes:
                raise ValueError('Invalid or unbounded compact binding count')
            for binding in entries:
                shape(binding, 'kind value start_byte scope hoisted')
                integer(binding['start_byte'], 0, size)
                boolean(binding['hoisted'])
                if type(binding['scope']) is not int or binding['scope'] != index:
                    raise ValueError('Foreign compact binding scope')
                value, kind = binding['value'], binding['kind']
                if kind == 'definition':
                    if type(value) is not str or value not in by_id:
                        raise ValueError('Foreign compact binding definition')
                elif kind == 'alias':
                    value = expression(value)
                    if value.type != 'identifier':
                        raise ValueError('Unsupported compact alias expression')
                elif kind == 'import':
                    if import_spec(value) not in import_keys:
                        raise ValueError('Foreign compact binding import')
                elif kind == 'unknown':
                    string(value)
                else:
                    raise ValueError('Invalid compact binding kind')
                scope.add(name, Binding(kind, value, Position(binding['start_byte']), scope, binding['hoisted']))
    sequence(payload['candidates'], budget.max_nodes)
    candidates, site_ids = [], set()
    for item in payload['candidates']:
        shape(item, 'scope callee fact')
        integer(item['scope'], 0, len(scopes) - 1)
        fact = item['fact']
        shape(fact, 'id path language role range text caller syntax_role provenance')
        source_fact(fact)
        scope = scopes[item['scope']]
        owner(fact['caller'])
        if fact['caller'] != scope.owner or fact['role'] not in ('call', 'reference') or fact['syntax_role'] not in ('call', 'constructor', 'call_or_conversion'):
            raise ValueError('Invalid compact site scope/role')
        expected = f'{record["path"]}:{fact["range"]["start_byte"]}:{fact["range"]["end_byte"]}:{fact["role"]}'
        if fact['id'] != expected or fact['id'] in site_ids:
            raise ValueError('Duplicate or forged compact site ID')
        site_ids.add(fact['id'])
        callee = expression(item['callee'])
        if not fact['range']['start_byte'] <= callee.start_byte <= callee.end_byte <= fact['range']['end_byte']:
            raise ValueError('Compact callee outside site range')
        candidates.append((fact, callee, scope))
    sequence(payload['errors'], budget.max_nodes)
    for item in payload['errors']:
        shape(item, 'kind range syntax_kind')
        if item['kind'] not in ('error', 'missing'):
            raise ValueError('Invalid compact parse error')
        location(item['range'])
        string(item['syntax_kind'])
    boolean(payload['partial'])
    shape(payload['counts'], 'nodes definitions')
    integer(payload['counts']['nodes'], 1, budget.max_nodes)
    if type(payload['counts']['definitions']) is not int or payload['counts']['definitions'] != len(definitions):
        raise ValueError('Invalid compact resource counts')
    if max(len(scopes) - 1, len(candidates), binding_count, len(payload['errors'])) > payload['counts']['nodes']:
        raise ValueError('Compact counts understate collected items')
    syntax = payload['syntax_metadata']
    shape(syntax, 'package_clauses go_control_directive go_bodyless_function go_cgo_import calls import_contexts python_declarations python_assignments python_dictionaries')
    sequence(syntax['package_clauses'], budget.max_nodes)
    for item in syntax['package_clauses']:
        shape(item, 'name range')
        string(item['name'])
        location(item['range'])
    for key in ('go_control_directive', 'go_bodyless_function', 'go_cgo_import'):
        boolean(syntax[key])
    if record['language'] != 'go' and (syntax['package_clauses'] or any(syntax[k] for k in
            ('go_control_directive', 'go_bodyless_function', 'go_cgo_import'))):
        raise ValueError('Foreign compact language metadata')
    for key in ('calls', 'import_contexts', 'python_declarations', 'python_assignments', 'python_dictionaries'):
        sequence(syntax[key], budget.max_nodes)
    if len(syntax['import_contexts']) != len(payload['imports']):
        raise ValueError('Compact import context inventory differs')
    for index, row in enumerate(syntax['import_contexts']):
        shape(row, 'ordinal scope conditional')
        if type(row['ordinal']) is not int or row['ordinal'] != index:
            raise ValueError('Foreign compact import context')
        integer(row['scope'], 0, len(scopes) - 1); boolean(row['conditional'])
        region = payload['imports'][index]['range']; scope = scopes[row['scope']]
        if not scope.range['start_byte'] <= region['start_byte'] <= region['end_byte'] <= scope.range['end_byte']:
            raise ValueError('Compact import outside its owning scope')
        if record['language'] != 'python' and row['conditional']:
            raise ValueError('Foreign compact import condition')
    sites = {fact['id']: (fact, scope) for fact, _, scope in candidates}
    seen = set()
    for row in syntax['calls']:
        shape(row, 'site arguments receiver_root')
        string(row['receiver_root'])
        if record['language'] != 'python' and row['receiver_root']:
            raise ValueError('Foreign compact receiver syntax')
        if row['site'] not in sites or sites[row['site']][0]['role'] != 'call' or row['site'] in seen:
            raise ValueError('Foreign or duplicate compact argument owner')
        seen.add(row['site'])
        fact, _ = sites[row['site']]
        sequence(row['arguments'], budget.max_nodes)
        previous = fact['range']['start_byte']
        for argument in row['arguments']:
            shape(argument, 'name expression container_references elements')
            string(argument['name'])
            value = expression(argument['expression'])
            if not previous <= value.start_byte <= value.end_byte <= fact['range']['end_byte']:
                raise ValueError('Compact argument outside its call or source order')
            container_references(argument['container_references'], value.start_byte, value.end_byte)
            sequence(argument['elements'], budget.max_nodes)
            if argument['elements'] and value.type not in ('list', 'tuple'):
                raise ValueError('Compact argument elements require a literal container')
            previous_element = value.start_byte
            for element in argument['elements']:
                item = expression(element)
                if not previous_element <= item.start_byte < item.end_byte <= value.end_byte:
                    raise ValueError('Compact argument element outside value')
                previous_element = item.end_byte
            previous = value.end_byte
    if seen != {id for id, (fact, _) in sites.items() if fact['role'] == 'call'}:
        raise ValueError('Compact call argument inventory differs')
    if record['language'] != 'python' and (syntax['python_declarations'] or syntax['python_assignments'] or syntax['python_dictionaries']):
        raise ValueError('Foreign compact Python syntax')
    seen = set()
    for row in syntax['python_declarations']:
        shape(row, 'id scope decorated conditional bases decorators header decorator_stack')
        if row['id'] not in by_id or row['id'] in seen:
            raise ValueError('Foreign or duplicate compact declaration syntax')
        seen.add(row['id'])
        integer(row['scope'], 0, len(scopes) - 1)
        boolean(row['decorated']); boolean(row['conditional'])
        fact, scope = by_id[row['id']], scopes[row['scope']]
        if not scope.range['start_byte'] <= fact['range']['start_byte'] < fact['range']['end_byte'] <= scope.range['end_byte']:
            raise ValueError('Compact declaration outside its owning scope')
        sequence(row['bases'], budget.max_nodes)
        if row['bases'] and fact['kind'] != 'class':
            raise ValueError('Compact bases belong only to classes')
        for base in row['bases']:
            value = expression(base)
            if not fact['range']['start_byte'] <= value.start_byte < value.end_byte <= fact['range']['end_byte']:
                raise ValueError('Compact base outside declaration')
        sequence(row['decorators'], budget.max_nodes)
        if row['decorated'] != bool(row['decorators']):
            raise ValueError('Compact decorator inventory differs')
        previous = scope.range['start_byte']
        for decorator in row['decorators']:
            shape(decorator, 'range text value callee')
            location(decorator['range']); string(decorator['text'])
            region = decorator['range']
            if (not previous <= region['start_byte'] < region['end_byte'] <= fact['range']['start_byte'] or
                    len(decorator['text'].encode()) != region['end_byte'] - region['start_byte']):
                raise ValueError('Compact decorator outside declaration envelope')
            for name in ('value', 'callee'):
                value = expression(decorator[name])
                if not region['start_byte'] <= value.start_byte < value.end_byte <= region['end_byte']:
                    raise ValueError('Compact decorator expression outside annotation')
            previous = region['end_byte']
        for name in ('header', 'decorator_stack'):
            fragment = row[name]
            if fragment is None: continue
            shape(fragment, 'range text'); location(fragment['range']); string(fragment['text'])
            region = fragment['range']
            if (not scopes[row['scope']].range['start_byte'] <= region['start_byte'] < region['end_byte'] <=
                    (fact['range']['end_byte'] if name == 'header' else fact['range']['start_byte']) or
                    len(fragment['text'].encode()) != region['end_byte'] - region['start_byte']):
                raise ValueError('Compact declaration fragment outside source')
        if (bool(row['decorator_stack']) != bool(row['decorators']) or
                fact['kind'] == 'class' and row['header'] is None and not payload['partial'] or
                row['header'] is not None and (fact['kind'] != 'class' or row['header']['range']['start_byte'] != fact['range']['start_byte']) or
                row['decorator_stack'] is not None and any(row['decorator_stack']['range'][key] != source['range'][key]
                    for key, source in (('start_byte', row['decorators'][0]), ('end_byte', row['decorators'][-1])))):
            raise ValueError('Compact declaration fragment ownership differs')
    if record['language'] == 'python' and seen != {id for id, fact in by_id.items() if fact['kind'] in ('class', 'function', 'method')}:
        raise ValueError('Compact declaration syntax inventory differs')
    seen = set()
    for row in syntax['python_assignments']:
        shape(row, 'range scope name conditional kind left right elements text container_references')
        location(row['range']); integer(row['scope'], 0, len(scopes) - 1)
        string(row['name']); boolean(row['conditional'])
        string(row['text'])
        if len(row['text'].encode('utf-8')) != row['range']['end_byte'] - row['range']['start_byte']:
            raise ValueError('Compact assignment text differs from source range')
        if row['kind'] not in ('assignment', 'augmented_assignment') or scopes[row['scope']].kind not in ('module', 'class'):
            raise ValueError('Invalid compact source assignment')
        left = expression(row['left'])
        if (not row['range']['start_byte'] <= left.start_byte < left.end_byte <= row['range']['end_byte'] or
                row['name'] != (left.spelling if left.type == 'identifier' else '')):
            raise ValueError('Compact assignment target differs')
        key = row['range']['start_byte'], row['range']['end_byte']
        if key in seen:
            raise ValueError('Duplicate compact assignment')
        seen.add(key)
        scope = scopes[row['scope']]
        if not scope.range['start_byte'] <= key[0] < key[1] <= scope.range['end_byte']:
            raise ValueError('Compact assignment outside its scope')
        right = expression(row['right'])
        if not key[0] <= right.start_byte <= right.end_byte <= key[1]:
            raise ValueError('Compact assignment value outside source range')
        container_references(row['container_references'], right.start_byte, right.end_byte)
        sequence(row['elements'], budget.max_nodes)
        if row['elements'] and right.type not in ('list', 'tuple'):
            raise ValueError('Compact assignment elements require a literal container')
        previous = right.start_byte
        for element in row['elements']:
            value = expression(element)
            if not previous <= value.start_byte <= value.end_byte <= right.end_byte:
                raise ValueError('Compact container element outside value or source order')
            previous = value.end_byte
    for row in syntax['python_dictionaries']:
        shape(row, 'range text pairs literal')
        boolean(row['literal'])
        location(row['range']); string(row['text']); sequence(row['pairs'], budget.max_nodes)
        region = row['range']
        if len(row['text'].encode()) != region['end_byte'] - region['start_byte']:
            raise ValueError('Compact dictionary text differs')
        previous = region['start_byte']
        for pair in row['pairs']:
            shape(pair, 'range text key value elements')
            location(pair['range']); string(pair['text']); sequence(pair['elements'], budget.max_nodes)
            span = pair['range']
            if not previous <= span['start_byte'] < span['end_byte'] <= region['end_byte'] or len(pair['text'].encode()) != span['end_byte'] - span['start_byte']:
                raise ValueError('Compact dictionary pair outside source')
            for item in (pair['key'], pair['value'], *pair['elements']):
                value = expression(item)
                if not span['start_byte'] <= value.start_byte < value.end_byte <= span['end_byte']:
                    raise ValueError('Compact dictionary operand outside pair')
            operand = Expression(**pair['value'])
            if pair['elements'] and operand.type not in ('list','tuple'):
                raise ValueError('Compact dictionary elements require a literal container')
            cursor = operand.start_byte
            for item in pair['elements']:
                element = Expression(**item)
                if not cursor <= element.start_byte < element.end_byte <= operand.end_byte:
                    raise ValueError('Compact dictionary element ownership or order differs')
                cursor = element.end_byte
            previous = span['end_byte']
    work.check()
    result = CollectedFile(dict(record), scopes, definitions, candidates, payload['imports'],
                         payload['errors'], payload['partial'], dict(payload['counts']), syntax,
                         collector_sha256=payload['collector_sha256'])
    if result.collected_fact_count > budget.max_facts:
        raise StopScan('fact_budget_exceeded')
    return result


class FileFacts:
    def __init__(self, record, raw, tree, work):
        self.record, self.raw, self.tree, self.work = record, raw, tree, work
        self.path, self.language = record['path'], record['language']
        self.module = Scope(None, 'module', '', range=span(raw, tree.root_node))
        self.scopes = [self.module]
        self.nodes = []
        self.definitions, self.sites, self.imports, self.errors = [], [], [], []
        self.callable_nodes = {}
        self.partial = tree.root_node.has_error
        self.initial_counts = work.nodes, work.facts
        self.syntax_metadata = {'package_clauses': [], 'go_control_directive': False,
                                'go_bodyless_function': False, 'go_cgo_import': False,
                                'calls': [], 'import_contexts': [], 'python_declarations': [], 'python_assignments': [], 'python_dictionaries': []}

    def operand(self, node):
        self.work.fact()
        self.work.text(node.end_byte - node.start_byte)
        return Expression.lower(self.raw, node).payload()

    def container_references(self, node):
        """Retain literal-container escapes without flattening route elements."""
        references, pending = [], [(node, False)]
        while pending:
            value, contained = pending.pop()
            self.work.node()
            parent = value.parent
            member = parent is not None and (
                parent.type == 'attribute' and parent.child_by_field_name('attribute') == value or
                parent.type == 'keyword_argument' and parent.child_by_field_name('name') == value)
            if contained and value.type == 'identifier' and not member:
                references.append(self.operand(value))
            contained = contained or value.type in ('list', 'tuple', 'dictionary', 'expression_list')
            pending.extend((child, contained) for child in reversed(value.named_children))
        return references

    def declaration_syntax(self, node, scope, definition):
        if self.language != 'python':
            return
        self.work.fact()
        bases = node.child_by_field_name('superclasses')
        decorators = []
        if node.parent and node.parent.type == 'decorated_definition':
            for decorator in node.parent.named_children:
                if decorator.type != 'decorator': continue
                self.work.fact(); self.work.text(decorator.end_byte - decorator.start_byte)
                value = decorator.named_children[0]
                decorators.append(dict(range=self.location(decorator), text=text(self.raw, decorator),
                    value=self.operand(value), callee=self.operand(value.child_by_field_name('function') if value.type == 'call' else value)))
        header, stack = None, None
        if node.type == 'class_definition':
            body = node.child_by_field_name('body')
            end = self.raw.rfind(b':', node.start_byte, body.start_byte) + 1 if body else node.start_byte
            if end > node.start_byte:
                self.work.fact(); self.work.text(end - node.start_byte)
                header = dict(range=configuration_span(self.raw, node.start_byte, end), text=self.raw[node.start_byte:end].decode())
        if decorators:
            a, b = decorators[0]['range']['start_byte'], decorators[-1]['range']['end_byte']
            self.work.fact(); self.work.text(b - a)
            stack = dict(range=dict(start_byte=a, end_byte=b, start_line=decorators[0]['range']['start_line'],
                                    end_line=decorators[-1]['range']['end_line']), text=self.raw[a:b].decode())
        self.syntax_metadata['python_declarations'].append({
            'id': definition['id'], 'scope': scope.ordinal,
            'decorated': bool(node.parent and node.parent.type == 'decorated_definition'),
            'decorators': decorators,
            'header': header, 'decorator_stack': stack,
            'conditional': self.conditional(node),
            'bases': [self.operand(base) for base in bases.named_children if base.type != 'comment'] if bases else []})

    def new_scope(self, parent, kind, name, owner=None, *, node):
        scope = Scope(parent, kind, name, owner, ordinal=len(self.scopes), range=self.location(node))
        self.scopes.append(scope)
        return scope

    def add_definition(self, node, name, kind, scope, binding_node=None, hoisted=False):
        self.work.fact()
        region = node
        if node.parent and node.parent.type == 'export_statement':
            region = node.parent
        end = region.end_byte
        # TS signatures own their optional terminator in the source declaration.
        if kind == 'method_declaration' and self.raw[end:end + 1] == b';':
            end += 1
        location = self.location(region, end)
        key = f'{self.path}:{location["start_byte"]}:{location["end_byte"]}'
        qualified = '.'.join(x for x in (scope.name, name) if x)
        self.work.text(end - location['start_byte'])
        definition = {'id': key, 'path': self.path, 'language': self.language,
                      'name': qualified, 'kind': kind, 'range': location,
                      'text': self.raw[location['start_byte']:end].decode('utf-8'),
                      'provenance': self.provenance(region.type),
                      'callable': kind in ('function', 'method', 'function_value', 'class')}
        self.definitions.append(definition)
        if binding_node is not False:
            conditional = self.language == 'python' and self.conditional(node)
            decorated = self.language == 'python' and node.parent and node.parent.type == 'decorated_definition'
            scope.add(name, Binding('unknown' if conditional or decorated else 'definition',
                                    'Python decorator transformation is not modeled' if decorated else
                                    'conditional Python declaration is not flow-resolved' if conditional else key,
                                    binding_node or node, scope, hoisted))
        return definition

    def location(self, node, end=None):
        return span(self.raw, node, end)

    @staticmethod
    def conditional(node):
        parent = node.parent
        while parent and parent.type not in ('function_definition', 'function_declaration',
                                             'class_definition', 'class_declaration', 'module', 'program'):
            if parent.type in ('if_statement', 'for_statement', 'while_statement', 'try_statement',
                               'except_clause', 'match_statement', 'case_clause'):
                return True
            parent = parent.parent
        return False

    def provenance(self, syntax_kind):
        return {'source_sha256': self.record['sha256'], 'rule_version': RULE_VERSION,
                'syntax_kind': syntax_kind, 'evidence_kind': 'static_syntax'}

    def collect(self):
        # Iterative walk avoids recursion proportional to arbitrary source depth.
        pending = [(self.tree.root_node, self.module)]
        while pending:
            node, scope = pending.pop()
            self.work.node()
            # ponytail: retain potential sites with their scopes, not every AST
            # node; full trees/bindings still live until cross-file resolution.
            if node.type in ('call', 'call_expression', 'new_expression', 'identifier') or (
                    self.language == 'go' and node.type == 'type_conversion_expression'):
                self.nodes.append((node, scope))
            if node.is_error or node.is_missing:
                self.errors.append({'kind': 'missing' if node.is_missing else 'error',
                                    'range': self.location(node), 'syntax_kind': node.type})
            child_scope = scope
            typ = node.type
            if self.language == 'python' and typ == 'dictionary' and node.parent and node.parent.type == 'expression_statement' and scope.kind == 'module':
                pairs = []
                self.work.fact(); self.work.text(node.end_byte - node.start_byte)
                for pair in node.named_children:
                    if pair.type != 'pair': continue
                    value = unwrap(pair.child_by_field_name('value'))
                    self.work.fact(); self.work.text(pair.end_byte - pair.start_byte)
                    pairs.append(dict(range=self.location(pair), text=text(self.raw, pair),
                        key=self.operand(pair.child_by_field_name('key')), value=self.operand(value),
                        elements=[self.operand(child) for child in value.named_children if child.type != 'comment'] if value.type in ('list', 'tuple') else []))
                self.syntax_metadata['python_dictionaries'].append(dict(range=self.location(node), text=text(self.raw, node), pairs=pairs,
                    literal=not self.conditional(node) and all(child.type in ('pair', 'comment') for child in node.named_children)))
            if self.language == 'go':
                if typ == 'package_clause':
                    self.syntax_metadata['package_clauses'].append({
                        'name': text(self.raw, node.named_children[-1]) if node.named_children else '',
                        'range': self.location(node)})
                elif typ == 'comment' and text(self.raw, node).lstrip().startswith(('//go:', '// +build')):
                    self.syntax_metadata['go_control_directive'] = True
                elif typ == 'function_declaration' and node.child_by_field_name('body') is None:
                    self.syntax_metadata['go_bodyless_function'] = True
            if typ in ('function_definition', 'function_declaration', 'method_declaration',
                       'method_definition'):
                name = text(self.raw, node.child_by_field_name('name'))
                kind = 'method' if typ in ('method_declaration', 'method_definition') or scope.kind == 'class' else 'function'
                if self.language == 'go' and typ == 'method_declaration':
                    receiver = node.child_by_field_name('receiver')
                    # Receiver spelling is provenance, not evidence of a resolved call.
                    types = [n for n in self.descendants(receiver) if n.type == 'type_identifier']
                    name = (text(self.raw, types[-1]) + '.' + name) if types else name
                definition = self.add_definition(node, name, kind, scope,
                                                 False if kind == 'method' else None,
                                                 self.language != 'python')
                self.declaration_syntax(node, scope, definition)
                lexical_parent = scope.parent if scope.kind == 'class' else scope
                child_scope = self.new_scope(lexical_parent, 'function', definition['name'], definition['id'], node=node)
                self.parameters(node, child_scope)
            elif typ in ('class_definition', 'class_declaration', 'interface_declaration'):
                name = text(self.raw, node.child_by_field_name('name'))
                kind = 'interface' if typ == 'interface_declaration' else 'class'
                definition = self.add_definition(node, name, kind, scope)
                self.declaration_syntax(node, scope, definition)
                child_scope = self.new_scope(scope, 'class', definition['name'], definition['id'], node=node)
            elif typ == 'type_declaration' and self.language == 'go':
                for spec in node.named_children:
                    name_node = spec.child_by_field_name('name')
                    type_node = spec.child_by_field_name('type')
                    if name_node:
                        kind = 'interface' if type_node and type_node.type == 'interface_type' else 'type'
                        self.add_definition(node if len(node.named_children) == 1 else spec,
                                            text(self.raw, name_node), kind, scope)
            elif typ == 'method_signature':
                self.add_definition(node, text(self.raw, node.child_by_field_name('name')),
                                    'method_declaration', scope, False)
            elif typ in ('arrow_function', 'function_expression', 'func_literal', 'lambda'):
                owner = self.callable_nodes.get(node.id)
                child_scope = self.new_scope(scope, 'function', owner['name'] if owner else scope.name,
                                    owner['id'] if owner else scope.owner, node=node)
                self.parameters(node, child_scope)
            elif typ in ('statement_block', 'block') and self.language != 'python':
                child_scope = self.new_scope(scope, 'block', scope.name, scope.owner, node=node)
            elif typ in ('global_statement', 'nonlocal_statement') and self.language == 'python':
                for name_node in node.named_children:
                    if name_node.type == 'identifier':
                        name = text(self.raw, name_node)
                        visible = scope.parent.lookup(name) if scope.parent else []
                        owner = self.module if typ == 'global_statement' else visible[0].scope if visible else scope
                        owner.add(name, Binding('unknown', 'global/nonlocal binding effects are not flow-modeled', name_node, owner))
            self.assignment(node, scope)
            self.import_binding(node, scope)
            pending.extend((child, child_scope) for child in reversed(node.children))

    def descendants(self, node):
        if node is None:
            return []
        result, pending = [], [node]
        while pending:
            item = pending.pop()
            self.work.node()
            result.append(item)
            pending.extend(reversed(item.named_children))
        return result

    def parameters(self, node, scope):
        params = node.child_by_field_name('parameters') or node.child_by_field_name('parameter')
        if params is None:
            return
        for child in params.named_children if params.type != 'identifier' else [params]:
            if self.language == 'go':
                pattern = child.child_by_field_name('name')
                patterns = [n for n in child.named_children if n.type == 'identifier'] if pattern else []
            elif child.type in ('required_parameter', 'optional_parameter'):
                patterns = identifiers(child.child_by_field_name('pattern'))
            elif child.type in ('default_parameter', 'typed_parameter', 'typed_default_parameter'):
                patterns = identifiers(child.child_by_field_name('name') or child.named_children[0])
            else:
                patterns = identifiers(child)
            for pattern in patterns:
                scope.add(text(self.raw, pattern), Binding('unknown', 'parameter value', pattern, scope))

    def assignment(self, node, scope):
        typ = node.type
        if typ not in ('assignment', 'augmented_assignment', 'assignment_expression',
                       'augmented_assignment_expression', 'short_var_declaration',
                       'assignment_statement', 'variable_declarator', 'var_spec', 'update_expression',
                       'delete_statement'):
            return
        left = node.child_by_field_name('left') or node.child_by_field_name('name') or node.child_by_field_name('argument')
        right = unwrap(node.child_by_field_name('right') or node.child_by_field_name('value'))
        names = identifiers(node if typ == 'delete_statement' else left)
        if (self.language == 'python' and scope.kind in ('module', 'class') and right is not None
                and typ in ('assignment', 'augmented_assignment')):
            self.work.fact()
            self.work.text(node.end_byte - node.start_byte)
            self.syntax_metadata['python_assignments'].append({
                'range': self.location(node), 'scope': scope.ordinal,
                'text': text(self.raw, node),
                'name': text(self.raw, left) if left is not None and left.type == 'identifier' else '',
                'conditional': self.conditional(node), 'kind': typ, 'left': self.operand(left), 'right': self.operand(right),
                'container_references': self.container_references(right),
                'elements': [self.operand(value) for value in right.named_children if value.type != 'comment']
                            if right.type in ('list', 'tuple') else []})
        for target in names:
            name = text(self.raw, target)
            owner_scope = scope
            if self.language in ('javascript', 'typescript') and typ == 'variable_declarator' and node.parent.type == 'variable_declaration':
                while owner_scope.parent and owner_scope.kind not in ('function', 'module'):
                    owner_scope = owner_scope.parent
            if typ in ('assignment_statement', 'assignment_expression', 'augmented_assignment_expression',
                       'update_expression', 'augmented_assignment'):
                visible = scope.lookup(name)
                owner_scope = visible[0].scope if visible else scope
            # Multiple assignments become unknown without a flow proof.
            if self.language == 'python' and self.conditional(node):
                owner_scope.add(name, Binding('unknown', 'conditional assignment is not flow-resolved', target, owner_scope))
            elif len(names) == 1 and right and right.type in ('arrow_function', 'function_expression', 'func_literal', 'lambda'):
                definition = self.add_definition(node, name, 'function_value', owner_scope, target)
                self.callable_nodes[right.id] = definition
            elif len(names) == 1 and right and right.type == 'identifier':
                owner_scope.add(name, Binding('alias', right, target, owner_scope))
            else:
                owner_scope.add(name, Binding('unknown', 'non-callable or unsupported assignment', target, owner_scope))

    def import_binding(self, node, scope):
        if scope.kind != 'module' and self.language != 'python':
            return
        specs = []
        if self.language == 'python' and node.type == 'import_statement':
            for child in node.named_children:
                module = child.child_by_field_name('name') if child.type == 'aliased_import' else child
                name = child.child_by_field_name('alias') or module
                specs.append((text(self.raw, name).split('.')[0], text(self.raw, module), None, name))
        if self.language == 'python' and node.type == 'import_from_statement':
            module = text(self.raw, node.child_by_field_name('module_name'))
            for child in node.named_children:
                if child.type == 'aliased_import':
                    name = text(self.raw, child.child_by_field_name('name'))
                    alias_node = child.child_by_field_name('alias') or child.child_by_field_name('name')
                    specs.append((text(self.raw, alias_node), module, name, alias_node))
                elif child.type == 'dotted_name' and child != node.child_by_field_name('module_name'):
                    specs.append((text(self.raw, child), module, text(self.raw, child), child))
                elif child.type == 'wildcard_import':
                    specs.append(('', module, '*', child))
        elif self.language in ('javascript', 'typescript') and node.type == 'import_statement':
            source = text(self.raw, node.child_by_field_name('source'))
            module = source[1:-1]
            for child in self.descendants(node):
                if child.type == 'import_specifier':
                    name = child.child_by_field_name('name')
                    alias = child.child_by_field_name('alias') or name
                    specs.append((text(self.raw, alias), module, text(self.raw, name), alias))
                elif child.type == 'namespace_import':
                    alias = child.named_children[-1]
                    specs.append((text(self.raw, alias), module, None, alias))
                elif child.type == 'import_clause':
                    for alias in child.named_children:
                        if alias.type == 'identifier':
                            specs.append((text(self.raw, alias), module, 'default', alias))
            if not specs:
                specs.append(('', module, None, node))
        elif self.language == 'go' and node.type == 'import_spec':
            module = text(self.raw, node.child_by_field_name('path'))[1:-1]
            alias = node.child_by_field_name('name')
            specs.append((text(self.raw, alias) if alias else module.rsplit('/', 1)[-1], module, None, alias or node))
        for name, module, symbol, binding_node in specs:
            self.work.fact()
            item = {'name': name, 'module': module, 'symbol': symbol, 'range': self.location(node),
                    'path': self.path, 'language': self.language, 'role': 'import',
                    'text': text(self.raw, node), 'provenance': self.provenance(node.type)}
            if self.language == 'go':
                item['explicit_alias'] = binding_node.type != 'import_spec'
            self.work.fact()
            self.syntax_metadata['import_contexts'].append({
                'ordinal': len(self.imports), 'scope': scope.ordinal,
                'conditional': self.language == 'python' and self.conditional(node)})
            self.imports.append(item)
            if not name:
                continue
            if self.language == 'go' and module == 'C':
                self.syntax_metadata['go_cgo_import'] = True
            unknown = ('local import unsupported' if scope.kind != 'module' else
                       'absolute Python import environment is not modeled' if self.language == 'python' and node.type == 'import_statement' else
                       'conditional import is not flow-resolved' if self.language == 'python' and self.conditional(node) else None)
            scope.add(name, Binding('unknown' if unknown else 'import', unknown or item,
                                    binding_node, scope, True))

    def lower(self):
        """Finish syntax-dependent decisions while this one file's Tree lives."""
        scopes = []
        for scope in self.scopes:
            self.work.check()
            scopes.append(Scope(scopes[scope.parent.ordinal] if scope.parent else None,
                                scope.kind, scope.name, scope.owner, ordinal=scope.ordinal, range=scope.range))
        for scope in self.scopes:
            copied = scopes[scope.ordinal]
            for name, entries in scope.bindings.items():
                copied.bindings[name] = [Binding(b.kind,
                    Expression.lower(self.raw, b.value) if b.kind == 'alias' else b.value,
                    Position(b.node.start_byte), scopes[b.scope.ordinal], b.hoisted) for b in entries]
        candidates = []
        for node, scope in self.nodes:
            self.work.check()
            callee = None
            syntax_role = 'call'
            if node.type in ('call', 'call_expression'):
                callee = node.child_by_field_name('function')
            elif node.type == 'new_expression':
                callee = node.child_by_field_name('constructor')
                syntax_role = 'constructor'
            elif self.language == 'go' and node.type == 'type_conversion_expression':
                callee = node.child_by_field_name('type')
                syntax_role = 'call_or_conversion'
            role = 'call' if callee else None
            if role is None and node.type == 'identifier':
                parent = node.parent
                # Bounded callable-value contexts, not every identifier reference.
                if parent and parent.type in ('return_statement', 'expression_list'):
                    is_return = parent.type == 'return_statement' or (parent.parent and parent.parent.type == 'return_statement')
                    is_alias = parent.type == 'expression_list' and parent.parent and parent.parent.type == 'short_var_declaration' and parent.parent.child_by_field_name('right') == parent
                    if is_return or is_alias:
                        role = 'reference'
                elif parent and parent.type in ('assignment', 'variable_declarator') and parent.child_by_field_name('right' if parent.type == 'assignment' else 'value') == node:
                    role = 'reference'
                if role:
                    callee = node
            if not role:
                continue
            self.work.text(node.end_byte - node.start_byte)
            fact = {'id': f'{self.path}:{node.start_byte}:{node.end_byte}:{role}',
                               'path': self.path, 'language': self.language, 'role': role,
                               'range': self.location(node), 'text': text(self.raw, node),
                               'caller': scope.owner, 'syntax_role': syntax_role,
                               'provenance': self.provenance(node.type)}
            candidates.append((fact, Expression.lower(self.raw, callee), scopes[scope.ordinal]))
            if role == 'call':
                self.work.fact()
                arguments = node.child_by_field_name('arguments')
                values = []
                for argument in arguments.named_children if arguments else []:
                    if argument.type == 'comment':
                        continue
                    value = (argument.child_by_field_name('value') or argument) if argument.type == 'keyword_argument' else argument
                    values.append({'name': text(self.raw, argument.child_by_field_name('name'))
                                   if argument.type == 'keyword_argument' else '',
                                   'expression': self.operand(value),
                                   'elements': [self.operand(child) for child in value.named_children if child.type != 'comment'] if value.type in ('list', 'tuple') else [],
                                   'container_references': self.container_references(value) if self.language == 'python' else []})
                receiver = callee
                while self.language == 'python' and receiver is not None and receiver.type in ('attribute', 'subscript', 'call'):
                    self.work.node()
                    receiver = receiver.child_by_field_name('object') or receiver.child_by_field_name('value') or receiver.child_by_field_name('function')
                self.syntax_metadata['calls'].append({'site': fact['id'], 'arguments': values,
                    'receiver_root': text(self.raw, receiver) if self.language == 'python' and receiver is not None and receiver.type == 'identifier' else ''})
        counts = {'nodes': self.work.nodes - self.initial_counts[0],
                  'definitions': len(self.definitions)}
        result = CollectedFile(self.record, scopes, self.definitions, candidates,
                               self.imports, self.errors, self.partial, counts, self.syntax_metadata)
        # No JSON byte buffer is retained by extraction; counting also bounds
        # strings/links which are not legacy body text. Encoding is bounded first.
        self.work.retain(_json_bytes(result.payload(), self.work.budget, self.work.cancel, measure=True))
        return result

    def release(self):
        # Scope/Binding cycles can otherwise hold native Nodes after this frame.
        for scope in self.scopes:
            scope.bindings.clear()
        self.nodes.clear()
        self.callable_nodes.clear()
        self.scopes.clear()
        self.raw = self.tree = None


def go_file_scope(file):
    """Source membership only: never select a platform, tags, CGO or toolchain."""
    clauses = file.syntax_metadata['package_clauses']
    if file.partial or len(clauses) != 1 or not clauses[0]['name']:
        return None, 'Go package declaration or parse is unqualified'
    if go_filename_class(file.path) != 'neutral':
        return None, 'Go filename/test/platform selection is unqualified'
    if any(file.syntax_metadata[k] for k in ('go_control_directive', 'go_bodyless_function', 'go_cgo_import')):
        return None, 'Go build/compiler directive, bodyless declaration or CGO selection is unqualified'
    return clauses[0]['name'], ''


def inventoried_go_package(directory, files):
    if hasattr(files, 'go_package'):
        return files.go_package(directory)
    names, bindings, reason = set(), {}, ''
    for path in files:
        if posixpath.normpath(posixpath.dirname(path)) != posixpath.normpath(directory) or files[path].language != 'go':
            continue
        file = files[path]
        name, failure = go_file_scope(file)
        names.add(name)
        reason = failure or reason
        for symbol, entries in file.module.bindings.items():
            count = sum(b.kind != 'import' for b in entries)
            if count:
                prior = bindings.get(symbol, (0, path))
                bindings[symbol] = (prior[0] + count, prior[1])
    if len(names) != 1 or None in names:
        reason = reason or 'Go package names differ in inventoried source'
    return {'name': next(iter(names)) if len(names) == 1 else None,
            'reason': reason, 'qualified': bool(names) and not reason, 'bindings': bindings}


def _python_parent(path, module):
    parent = posixpath.dirname(path)
    for _ in range(len(module) - len(module.lstrip('.')) - 1):
        if not parent:
            return None
        parent = posixpath.dirname(parent)
    return parent


def module_paths(file, spec, files, configurations, context=None):
    module, symbol = spec['module'], spec['symbol']
    parent = posixpath.dirname(file.path)
    if file.language == 'python':
        if not module.startswith('.'):
            return [], symbol, 'absolute Python import environment is not modeled'
        count = len(module) - len(module.lstrip('.'))
        parent = _python_parent(file.path, module)
        if parent is None:
            return [], symbol, 'relative Python import escapes the admitted source root'
        stem = posixpath.join(parent, module[count:].replace('.', '/'))
        paths = [stem + '.py', posixpath.join(stem, '__init__.py')]
    elif file.language in ('javascript', 'typescript'):
        if not module.startswith('.'):
            return [], symbol, 'package import/export resolution is not modeled'
        stem = posixpath.normpath(posixpath.join(parent, module))
        paths = [stem] if PurePosixPath(stem).suffix else [stem + ext for ext in ('.ts', '.tsx', '.js', '.jsx')] + [posixpath.join(stem, 'index' + ext) for ext in ('.ts', '.js')]
    else:
        paths = []
        if context is not None:
            owner = context['owners'][file.path]['repository_id'], context['owners'][file.path]['revision']
            caller = context['modules'].get(owner)
            if caller is None:
                return [], symbol, 'Go module/control variant is unqualified'
            prefix = caller['module']
            if module == prefix or module.startswith(prefix + '/'):
                # A nested required/registered module wins no implicit own-prefix
                # shortcut. Source directories and symbol names cannot choose it.
                domains = set(caller['requirements']) | {entry['module_path'] for entry in context['dependencies'] if entry['consumer'] == owner}
                if any(module == domain or module.startswith(domain + '/') for domain in domains):
                    return [], symbol, 'Overlapping required/registered module leaves own-source import ambiguous'
                provider = owner
            else:
                if not caller['external_qualified']:
                    return [], symbol, 'Consumer workspace/vendor/replacement dependency selection is unqualified'
                matches = [entry for entry in context['dependencies'] if entry['consumer'] == owner and
                           (module == entry['module_path'] or module.startswith(entry['module_path'] + '/'))]
                if len(matches) != 1 or not matches[0]['qualified']:
                    return [], symbol, 'No unique qualified dependency registration for this consumer snapshot'
                provider, prefix = matches[0]['provider'], matches[0]['module_path']
            directory = module[len(prefix):].lstrip('/')
            if directory and (posixpath.normpath(directory) != directory or any(x in ('', '.', '..') for x in directory.split('/'))):
                return [], symbol, 'Noncanonical Go import directory'
            package = context['packages'].get((provider, directory))
            if package is None or not package['qualified']:
                reason = '; '.join(package.get('uncertainty', [])) if package else ''
                return [], symbol, reason or 'Complete non-test package inventory is unavailable or a build variant is unqualified'
            if spec['name'] in ('.', '_') or (not spec.get('explicit_alias') and spec['name'] != package['name']):
                return [], symbol, 'Default package name, dot import or blank import is unsupported'
            return package['paths'], symbol, ''
        for config_path, content in configurations.items():
            parsed = go_module(content, Work(Budget(), None))
            if parsed is None:
                continue
            prefix = parsed['module']
            if module == prefix or module.startswith(prefix + '/'):
                if any(module == domain or module.startswith(domain + '/') for domain in parsed['requirements']):
                    return [], symbol, 'Overlapping required module leaves own-source import ambiguous'
                directory = posixpath.normpath(posixpath.join(posixpath.dirname(config_path), module[len(prefix):].lstrip('/')))
                paths.extend(files.in_directory(directory) if hasattr(files, 'in_directory') else
                             (path for path in files if posixpath.normpath(posixpath.dirname(path)) == directory and path.endswith('.go')))
    admitted = sorted(set(path for path in paths if
        (files.inventoried(path) if hasattr(files, 'inventoried') else path in files)))
    found = [path for path in admitted if path in files and files[path].language == file.language]
    if admitted != found:
        return [], symbol, 'import alternatives include unparsed, excluded or unsupported source'
    if context is not None:
        owner = context['owners'][file.path]['repository_id'], context['owners'][file.path]['revision']
        found = [path for path in found if (context['owners'][path]['repository_id'], context['owners'][path]['revision']) == owner]
    return found, symbol, '' if found else 'import target absent from guarded source inventory'


def django_registration_context(value, work):
    """Trusted caller enrollment, never a repository file or a source-gold key."""
    if value is None:
        return None
    work.check()
    odoo = type(value) is dict and value.get('framework_id') == 'odoo'
    fields = {'schema_version', 'enabled', 'framework_id', 'policy_id', 'consumer', 'dependency', 'source_roots'}
    if odoo: fields |= {'ownership', 'configurations'}
    if (type(value) is not dict or set(value) != fields or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or
            value['enabled'] is not True or value['framework_id'] not in ('django', 'odoo') or
            value['policy_id'] != ('odoo-hooks-finite-v1' if odoo else 'django-registration-finite-v1')):
        raise ValueError('Explicit finite framework registration enrollment required')
    def token(value):
        return type(value) is str and 1 <= len(value.encode('utf-8')) <= 128 and not any(ord(c) < 32 for c in value)
    for name in ('consumer', 'dependency'):
        row = value[name]
        fields = {'repository_id', 'revision', 'source_root_id'}
        if name == 'dependency': fields |= {'module_prefix', 'identity_kind'}
        elif odoo: fields |= {'consumer_id', 'service_id'}
        if type(row) is not dict or set(row) != fields or not all(token(row[k]) for k in fields):
            raise ValueError('Typed framework snapshot identity required')
        if len(row['revision']) not in (40, 64) or any(c not in '0123456789abcdef' for c in row['revision']):
            raise ValueError('Pinned framework source revision required')
    dependency = value['dependency']
    if dependency['module_prefix'] != value['framework_id'] or dependency['identity_kind'] not in (
            'synthetic_fixture', 'declared_dependency_snapshot'):
        raise ValueError('Finite framework source identity required')
    roots = value['source_roots']
    if type(roots) is not list or not 1 <= len(roots) <= 16:
        raise ValueError('Bounded framework source-root enrollment required')
    ids, prefixes = set(), set()
    for root in roots:
        work.check()
        if (type(root) is not dict or set(root) != {'id', 'source_prefix', 'ownership'} or
                not token(root['id']) or root['id'] in ids or
                root['ownership'] != 'one_explicit_admitted_snapshot'):
            raise ValueError('Unique owned framework source roots required')
        prefix = root['source_prefix']
        if type(prefix) is not str or len(prefix.encode('utf-8')) > 4096 or prefix in prefixes:
            raise ValueError('Canonical framework source prefix required')
        if prefix:
            if not prefix.endswith('/') or str(PurePosixPath(prefix[:-1])) != prefix[:-1]:
                raise ValueError('Canonical framework source prefix required')
            try: SourceRoot.parts(prefix[:-1])
            except OSError as error: raise ValueError('Framework source prefix escapes enrollment') from error
        ids.add(root['id']); prefixes.add(prefix)
    if value['consumer']['source_root_id'] not in ids or dependency['source_root_id'] not in ids:
        raise ValueError('Framework identity refers to an unenrolled source root')
    if value['consumer']['source_root_id'] == dependency['source_root_id'] and any(
            value['consumer'][key] != dependency[key] for key in ('repository_id', 'revision')):
        raise ValueError('One enrolled snapshot cannot have conflicting identities')
    if odoo:
        owners, paths = value['ownership'], set()
        if type(owners) is not list or not 1 <= len(owners) <= 32:
            raise ValueError('Bounded explicit Odoo ownership required')
        for owner in owners:
            work.node()
            if (type(owner) is not dict or set(owner) != {'path', 'consumer_id', 'service_id', 'configuration_namespace'} or
                    not all(token(owner[k]) for k in ('consumer_id', 'service_id')) or
                    owner['configuration_namespace'] is not None and not token(owner['configuration_namespace'])):
                raise ValueError('Typed Odoo consumer/service/namespace ownership required')
            path = owner['path']
            if type(path) is not str or not path or len(path.encode()) > 4096 or path in paths:
                raise ValueError('Unique Odoo source ownership path required')
            canonical = path[:-1] if path.endswith('/') else path
            SourceRoot.parts(canonical)
            if str(PurePosixPath(canonical)) != canonical:
                raise ValueError('Canonical Odoo ownership path required')
            consumer_prefix = next(root['source_prefix'] for root in roots if root['id'] == value['consumer']['source_root_id'])
            if not path.startswith(consumer_prefix):
                raise ValueError('Odoo ownership lies outside the consumer source root')
            paths.add(path)
        configurations = value['configurations']
        if type(configurations) is not list or len(configurations) > 32:
            raise ValueError('Bounded Odoo configuration enrollment required')
        seen = set()
        for configuration in configurations:
            work.node()
            if type(configuration) is not dict or set(configuration) != {'path', 'manifest_path'}:
                raise ValueError('Explicit Odoo configuration/manifest pair required')
            for key in ('path', 'manifest_path'):
                path = configuration[key]
                if type(path) is not str or len(path.encode()) > 4096 or str(PurePosixPath(path)) != path:
                    raise ValueError('Canonical Odoo configuration path required')
                SourceRoot.parts(path)
            if configuration['path'] in seen or not configuration['path'].endswith('.xml') or not configuration['manifest_path'].endswith('.py'):
                raise ValueError('Unique finite Odoo configuration role required')
            owner = odoo_owner(configuration['path'], value)
            manifest_owner = odoo_owner(configuration['manifest_path'], value)
            if (owner is None or manifest_owner is None or owner['configuration_namespace'] is None or
                    any(owner[k] != manifest_owner[k] for k in ('consumer_id', 'service_id', 'configuration_namespace'))):
                raise ValueError('Configuration and manifest ownership differ')
            seen.add(configuration['path'])
    raw = _json_bytes(value, work.budget, work.cancel)
    if len(raw) > 16384:
        raise StopScan('framework_context_byte_budget_exceeded')
    work.retain(len(raw))
    return json.loads(raw)


def odoo_owner(path, enrollment):
    owners = [row for row in enrollment['ownership'] if path == row['path'] or
              row['path'].endswith('/') and path.startswith(row['path'])]
    return max(owners, key=lambda row: len(row['path'])) if owners else None


def configuration_span(raw, start, end):
    line = raw[:start].count(b'\n') + 1
    return dict(start_byte=start, end_byte=end, start_line=line,
                end_line=max(line, raw[:end].count(b'\n') + (not raw[:end].endswith(b'\n'))))


def odoo_configuration(record, raw, work):
    """Target-free XML facts inside the guarded structural source-read owner.

    Expat owns structure. Byte delimiters only locate its already recognized
    tags; ambiguous delimiter/entity forms are withheld, never interpreted.
    """
    work.check()
    raw.decode('utf-8')
    parser = expat.ParserCreate('UTF-8')
    records, stack = [], []
    def fragment(start, end):
        work.fact(); work.text(end - start)
        return dict(range=configuration_span(raw, start, end), text=raw[start:end].decode())
    def reject(*args): raise ValueError('unsupported_xml_declaration_or_entity')
    def declaration(version, encoding, standalone):
        work.node()
        if encoding is not None and encoding.lower().replace('-', '') != 'utf8': reject()
    def start(name, attributes):
        work.node()
        if len(stack) >= 128: raise StopScan('configuration_depth_budget_exceeded')
        if (any('<' in value or '>' in value for value in attributes.values()) or
                any(key == 'xmlns' or key.startswith('xmlns:') for key in attributes)): reject()
        a = parser.CurrentByteIndex
        b = raw.find(b'>', a) + 1
        if b <= a: reject()
        if stack: stack[-1]['children'] = True
        work.text(len(name.encode()) + sum(len(k.encode()) + len(v.encode()) for k, v in attributes.items()))
        stack.append(dict(name=name, attributes=dict(attributes), start=a, content=b,
                          children=False, fields=[], ambiguous=name == 'record' and (not stack or
                              any(parent['name'] not in ('odoo','data') for parent in stack))))
        if len(stack) > 1 and stack[-2]['name'] == 'record' and name != 'field':
            stack[-2]['ambiguous'] = True
    def end(name):
        work.node()
        node = stack.pop()
        if node['name'] != name: reject()
        a = parser.CurrentByteIndex
        empty = raw[node['start']:node['content']].rstrip().endswith(b'/>')
        b = node['content'] if empty else raw.find(b'>', a) + 1
        if b < a and not empty: reject()
        value = dict(fragment(node['start'], b), attributes=node['attributes'], children=node['children'],
                     content=fragment(node['content'], node['content'] if empty else a))
        if name == 'field' and stack and stack[-1]['name'] == 'record':
            stack[-1]['fields'].append(value)
        elif name == 'record':
            # Namespace identity ambiguity spans all physical record models;
            # hook publication separately admits only ir.cron declarations.
            if len(records) >= min(work.budget.max_facts, 4096): raise StopScan('configuration_record_budget_exceeded')
            value['fields'] = node['fields']; value['ambiguous'] = node['ambiguous']; records.append(value)
    def data(value):
        work.node()
    def markup(*args):
        work.node()
        if stack: stack[-1]['children'] = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.CommentHandler = markup
    parser.ProcessingInstructionHandler = markup
    parser.StartCdataSectionHandler = markup
    parser.EndCdataSectionHandler = markup
    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SkippedEntityHandler = reject
    errors = []
    try:
        if b'&' in raw: reject()
        # Chunking ensures the enclosing deadline/cancellation also bounds
        # parser work between callbacks, including long uninterpreted text.
        for offset in range(0, len(raw), 65536):
            work.check(); parser.Parse(raw[offset:offset + 65536], False)
        parser.Parse(b'', True); work.check()
    except (ValueError, expat.ExpatError) as error:
        records = []
        errors = [dict(kind='unsupported_configuration', range=configuration_span(raw, 0, len(raw)))]
    return dict(record=record, collector_sha256=collector_identity(), records=records, errors=errors,
                partial=bool(errors))


def decode_configuration(encoded, expected_record, digest, raw, work):
    """Validate the captured configuration handoff at the shared IR boundary."""
    work.check()
    if (type(encoded) is not bytes or len(encoded) > work.budget.max_handoff_bytes or
            hashlib.sha256(encoded).hexdigest() != digest or expected_record['kind'] != 'configuration' or
            expected_record['language'] != 'configuration' or expected_record['bytes'] != len(raw) or
            source_hash(raw) != expected_record['sha256']):
        raise ValueError('Invalid configuration handoff identity')
    work.retain(len(encoded))
    def pairs(values):
        result = {}
        for key, value in values:
            work.node()
            if key in result: raise ValueError('Duplicate compact configuration field')
            result[key] = value
        return result
    def shape(value, fields):
        work.node()
        if type(value) is not dict or set(value) != set(fields.split()): raise ValueError('Invalid compact configuration shape')
    def fragment(value):
        shape(value,'range text')
        region = value['range']; shape(region,'start_byte end_byte start_line end_line')
        if (any(type(v) is not int for v in region.values()) or
                not 0 <= region['start_byte'] <= region['end_byte'] <= len(raw) or
                region != configuration_span(raw,region['start_byte'],region['end_byte']) or type(value['text']) is not str or
                value['text'].encode() != raw[region['start_byte']:region['end_byte']]):
            raise ValueError('Configuration fragment differs from captured source')
        work.fact(); work.text(len(value['text'].encode()))
        return region
    def value(row, fields):
        shape(row, fields)
        span = fragment({k:row[k] for k in ('range','text')})
        shape(row['attributes'], ' '.join(row['attributes']) if type(row['attributes']) is dict else '')
        if (any(type(k) is not str or type(v) is not str for k,v in row['attributes'].items()) or type(row['children']) is not bool):
            raise ValueError('Typed configuration attributes required')
        for k,v in row['attributes'].items(): work.node(); work.text(len(k.encode()) + len(v.encode()))
        content = fragment(row['content'])
        if not span['start_byte'] <= content['start_byte'] <= content['end_byte'] <= span['end_byte']:
            raise ValueError('Configuration content outside its owning element')
        return span
    try: payload = json.loads(encoded,object_pairs_hook=pairs,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid compact number')))
    except (UnicodeError,RecursionError,json.JSONDecodeError) as error: raise ValueError('Invalid compact configuration JSON') from error
    shape(payload,'record collector_sha256 records errors partial')
    if (payload['record'] != expected_record or payload['collector_sha256'] != collector_identity() or
            type(payload['records']) is not list or len(payload['records']) > min(work.budget.max_facts,4096) or
            type(payload['errors']) is not list or len(payload['errors']) > 1 or type(payload['partial']) is not bool or
            payload['partial'] != bool(payload['errors']) or payload['partial'] and payload['records']):
        raise ValueError('Foreign or stale configuration IR')
    for row in payload['records']:
        span = value(row,'range text attributes children content fields ambiguous')
        if (type(row['fields']) is not list or len(row['fields']) > work.budget.max_facts or
                type(row['ambiguous']) is not bool):
            raise ValueError('Invalid finite configuration record')
        previous = span['start_byte']
        for field in row['fields']:
            region = value(field,'range text attributes children content')
            if not previous <= region['start_byte'] < region['end_byte'] <= span['end_byte']:
                raise ValueError('Configuration field ownership or order differs')
            previous = region['end_byte']
    for error in payload['errors']:
        shape(error,'kind range')
        if error['kind'] != 'unsupported_configuration' or error['range'] != configuration_span(raw,0,len(raw)):
            raise ValueError('Invalid captured configuration error')
    work.check()
    return payload


def resolver(files, configurations, context=None, *, definitions=None, framework_context=None):
    # The persistent owner supplies indexed lookup; finite comparisons keep their mapping.
    if definitions is None:
        definitions = {definition['id']: definition for file in files.values() for definition in file.definitions}

    packages = {}

    def local_package(directory):
        if directory not in packages:
            if len(packages) >= 2:
                packages.pop(next(iter(packages)))
            packages[directory] = inventoried_go_package(directory, files)
        return packages[directory]

    def package_binding(directory, name, package):
        return files.go_binding(directory, name) if hasattr(files, 'go_binding') else package['bindings'].get(name, (0, None))

    def imported(file, item, member):
        if (file.language == 'python' and member is not None and item['symbol'] is not None and
                item['module'].startswith('.') and not item['module'].strip('.')):
            parent = _python_parent(file.path, item['module'])
            if parent is None:
                return [], 'relative Python import escapes the admitted source root', 'import_alias'
            # Package attributes can replace an imported child module; only the
            # inventoried namespace form is supported, not initializer execution.
            initializer = posixpath.join(parent, '__init__.py')
            initializer_present = files.inventoried(initializer) if hasattr(files, 'inventoried') else initializer in files
            if initializer_present:
                return [], 'relative package attributes require initializer resolution', 'import_alias'
            item = dict(item, module=item['module'] + item['symbol'], symbol=member)
        paths, symbol, reason = module_paths(file, item, files, configurations, context)
        # Export presence cannot choose an import module. An extension/search
        # policy must first identify one module independently of its symbols.
        # Go is the exception: one package directory can contain many files.
        if file.language != 'go' and len(paths) > 1:
            return [], 'multiple inventoried module paths; extension/package selection policy is not modeled', 'import_alias'
        if file.language == 'go' and len({posixpath.dirname(path) for path in paths}) > 1:
            return [], 'multiple package directories match module configurations', 'import_alias'
        if file.language == 'go' and context is None and paths:
            directory = posixpath.dirname(paths[0])
            package = local_package(directory)
            if not package['qualified']:
                return [], package['reason'], 'unsupported_go_context'
            if item['name'] in ('.', '_') or not item.get('explicit_alias') and item['name'] != package['name']:
                return [], 'Default Go package name, dot import or blank import is unsupported', 'import_alias'
        symbol = member if symbol is None else symbol
        if not symbol:
            return [], 'module value has no callable target', 'import_alias'
        targets = []
        if file.language == 'go' and context is None and paths and package_binding(directory, symbol, package)[0] != 1:
            return [], 'Package binding is absent or ambiguous in inventoried Go source', 'import_alias'
        if file.language == 'go' and context is not None and sum(len(files[path].module.bindings.get(symbol, [])) for path in paths) != 1:
            return [], reason or 'Package binding is absent or ambiguous across the complete non-test inventory', 'import_alias'
        for path in paths:
            other = files[path]
            bindings = other.module.bindings.get(symbol, [])
            # This baseline resolves only one ordinary source definition, not
            # reexports, conditional exports, overloads or partial files.
            if not other.partial and len(bindings) == 1 and bindings[0].kind == 'definition':
                target = definitions[bindings[0].value]
                exported = other.language not in ('javascript', 'typescript') or target['text'].startswith('export ')
                go_exported = other.language != 'go' or symbol[0].isupper()
                ordinary = (other.language != 'go' or
                            target['provenance']['syntax_kind'] == 'function_declaration')
                if target['callable'] and exported and go_exported and ordinary:
                    targets.append(target['id'])
        if len(targets) == 1:
            return targets, ('one source definition in an explicit declared snapshot; runtime/workspace/MVS unqualified' if context is not None else
                             'one source definition in an inventoried local module'), 'import_alias'
        return [], reason or 'import target is missing, partial, ambiguous or unsupported', 'import_alias'

    def resolve(file, node, scope, offset, seen=None):
        seen = set() if seen is None else seen
        if file.language == 'go' and context is None:
            directory = posixpath.dirname(file.path)
            package = local_package(directory)
            if not package['qualified']:
                return [], package['reason'], 'unsupported_go_context'
        if file.language == 'go' and context is not None:
            owner = context['owners'][file.path]['repository_id'], context['owners'][file.path]['revision']
            package = context['packages'].get((owner, posixpath.dirname(context['owners'][file.path]['physical_path'])))
            if context['modules'].get(owner) is None or package is None or not package['qualified']:
                reason = '; '.join(package.get('uncertainty', [])) if package else ''
                return [], reason or 'Go module/control or complete non-test package variant is unqualified', 'unsupported_go_context'
        if node is None:
            return [], 'callee unavailable', 'unknown'
        if node.type in ('attribute', 'member_expression', 'selector_expression'):
            if node.base_identifier:
                bindings = scope.lookup(node.base)
                if (len(bindings) == 1 and bindings[0].kind == 'import' and
                        (bindings[0].value['symbol'] is None or file.language == 'python' and
                         bindings[0].value['module'].startswith('.') and not bindings[0].value['module'].strip('.'))):
                    return imported(file, bindings[0].value, node.member)
            return [], 'receiver dispatch requires type/points-to analysis; candidates not enumerated', 'unsupported_receiver'
        if node.type != 'identifier':
            return [], 'computed or unsupported callee; possible targets not enumerated', 'unsupported_dynamic'
        name = node.spelling
        bindings = scope.lookup(name)
        if file.language == 'go' and context is None:
            count, path = package_binding(directory, name, package)
            if bindings and bindings[0].scope.kind == 'module' and bindings[0].kind != 'import' and count != 1:
                return [], 'Package value binding is ambiguous in inventoried Go source', 'lexical_unknown'
            if not bindings and count == 1:
                other = files[path]
                return resolve(other, node, other.module, offset, seen)
        if len(bindings) != 1:
            return [], 'binding absent or multiply assigned in this lexical scope', 'lexical_unknown'
        binding = bindings[0]
        if file.language == 'go' and context is not None and binding.scope.kind == 'module' and binding.kind != 'import':
            # Go package declarations share a namespace across physical files;
            # imports stay file-scoped. Duplicate package values cannot be exact.
            count = sum(1 for path in package['paths'] for entry in files[path].module.bindings.get(name, []) if entry.kind != 'import')
            if count != 1:
                return [], 'Package value binding is ambiguous across the complete non-test inventory', 'lexical_unknown'
        key = (id(binding.scope), name)
        if key in seen:
            return [], 'alias cycle; no exact target', 'alias_cycle'
        if not binding.hoisted and binding.node.start_byte > offset and binding.scope.kind != 'module':
            return [], 'use before a local declaration is not resolved', 'declaration_order'
        if binding.kind == 'definition':
            target = definitions[binding.value]
            if not target['callable']:
                return [], 'value is a type/interface declaration, not an identified callable', 'lexical_unknown'
            return [target['id']], ('one complete package value in an explicit declared snapshot; runtime/workspace/MVS unqualified' if file.language == 'go' and context is not None else 'one lexically visible source definition'), 'lexical_direct'
        if binding.kind == 'import':
            return imported(file, binding.value, None)
        if binding.kind == 'alias':
            seen.add(key)
            targets, reason, _ = resolve(file, binding.value, binding.scope, binding.node.start_byte, seen)
            return targets, reason, 'stable_value_alias'
        return [], str(binding.value), 'lexical_unknown'
    resolve.declared_snapshot_scope = context is not None
    resolve.inventoried_package_scope = context is None
    resolve.framework_context = framework_context
    if framework_context is not None:
        resolve.framework_sites = django_registration_resolver(files, definitions, resolve, framework_context)
    return resolve


def django_registration_resolver(files, definitions, lexical, enrollment):
    """Finite opt-in registrations over the existing target-free handoff.

    Registration witnesses never alter lexical resolution. No imports, fixture
    expected values, repository configuration or application code are executed.
    """
    roots = {row['id']: row['source_prefix'] for row in enrollment['source_roots']}
    consumer, dependency = enrollment['consumer'], enrollment['dependency']
    prefix = roots[dependency['source_root_id']]
    odoo = enrollment['framework_id'] == 'odoo'
    apis = {'django.urls.conf': {'path': 'django_route', 're_path': 'django_route'},
            'django.core.management.base': {'BaseCommand': 'django_management_handle'},
            'django.db.models.manager': {'Manager': 'django_orm_get_queryset'}}
    facades = {('django.urls', 'path'): ('django.urls.conf', 'path'),
               ('django.urls', 're_path'): ('django.urls.conf', 're_path'),
               ('django.db.models', 'Manager'): ('django.db.models.manager', 'Manager')}
    if odoo:
        apis = {'odoo.http': {'route': 'odoo_route_annotation'},
                'odoo.orm.models': {'Model': 'odoo_model_method_declaration'},
                'odoo.orm.decorators': {'model': 'odoo_model_method_declaration'}}
        facades = {('odoo.models', 'Model'): ('odoo.orm.models', 'Model'),
                   ('odoo.api', 'model'): ('odoo.orm.decorators', 'model')}

    def owned(path, identity):
        matches = [(len(p), key) for key, p in roots.items() if path.startswith(p)]
        return bool(matches) and max(matches)[1] == identity['source_root_id']

    def witness(file, row, role):
        identifier = row.get('id')
        if identifier is None:
            if row.get('role') == 'import':
                identifier = 'import:' + hashlib.sha256(_json_bytes([file.path, file.imports.index(row), row['range']], Budget(), None)).hexdigest()
            else:
                suffix = 'assignment' if row.get('kind') in ('assignment', 'augmented_assignment') else 'framework_witness'
                identifier = f'{file.path}:{row["range"]["start_byte"]}:{row["range"]["end_byte"]}:{suffix}'
        return {'id': identifier, 'path': file.path, 'range': row['range'],
                'source_sha256': file.record['sha256'], 'source_role': role}

    def later_wildcard(file, binding, work, evidence):
        for item, context in zip(file.imports, file.syntax_metadata['import_contexts']):
            work.check()
            if (item['symbol'] == '*' and context['scope'] == binding.scope.ordinal and
                    item['range']['start_byte'] > binding.node.start_byte):
                evidence.append(witness(file, item, 'wildcard_identity_boundary'))
                return True
        return False

    def import_hint(file, expression, scope, work, seen=None, depth=0):
        work.node()
        seen = set() if seen is None else seen
        name = expression.spelling if expression.type == 'identifier' else expression.base if expression.base_identifier else ''
        if not name:
            return None
        bindings = scope.lookup(name)
        if not bindings:
            return None
        if len(bindings) == 1 and bindings[0].kind == 'definition':
            definition = definitions[bindings[0].value]
            if definition['kind'] == 'class' and definition['id'] not in seen and depth < 32:
                seen.add(definition['id'])
                declaration = next(row for row in file.syntax_metadata['python_declarations'] if row['id'] == definition['id'])
                for base in declaration['bases']:
                    hint = import_hint(file, Expression(**base), file.scopes[declaration['scope']], work, seen, depth + 1)
                    if hint:
                        return (*hint[:4], 'transitive_framework_base_is_unqualified')
        owner = bindings[0].scope.ordinal
        for item, context in zip(file.imports, file.syntax_metadata['import_contexts']):
            work.check()
            if context['scope'] != owner or item['name'] != name:
                continue
            module, symbol = item['module'], item['symbol']
            if expression.type != 'identifier':
                if symbol is None:
                    symbol = expression.member
                elif (module, symbol) == ('django.db', 'models'):
                    module, symbol = 'django.db.models', expression.member
                elif odoo and module == 'odoo' and symbol in ('http', 'models', 'api'):
                    module, symbol = 'odoo.' + symbol, expression.member
                # A member of a callable (from_queryset, etc.) is a candidate,
                # never a direct API identity.
            physical = facades.get((module, symbol), (module, symbol))
            kind = apis.get(physical[0], {}).get(physical[1])
            if not kind and module.startswith('django.core.management.') and symbol == 'BaseCommand':
                kind = 'django_management_handle'
            if not kind and module.startswith('django.db.models.') and symbol == 'Manager':
                kind = 'django_orm_get_queryset'
            if kind:
                reason = ''
                if len(bindings) != 1 or context['conditional'] or owner != 0:
                    reason = 'shadowed_ambiguous_conditional_or_local_framework_import'
                elif later_wildcard(file, bindings[0], work, []):
                    reason = 'later_wildcard_framework_import'
                elif expression.type != 'identifier' and item['symbol'] not in ((None, 'http', 'models', 'api') if odoo else (None, 'models')):
                    reason = 'computed_framework_factory_or_member'
                if odoo and any(assignment['scope'] == owner and assignment['left']['type'] == 'attribute' and
                        assignment['left']['base_identifier'] and assignment['left']['base'] == name
                        for assignment in file.syntax_metadata['python_assignments']):
                    reason = 'mutated_framework_namespace_member'
                return module, symbol, kind, item, reason
        return None

    def module_file(module, work, witnesses, finite_parent=None):
        parts = module.split('.')
        if not parts or parts[0] != dependency['module_prefix']:
            return None, 'unenrolled_framework_module'
        for count in range(1, len(parts) + 1):
            work.check()
            stem = prefix + '/'.join(parts[:count])
            candidates = [stem + '.py', stem + '/__init__.py']
            inventoried = files.inventoried if hasattr(files, 'inventoried') else files.__contains__
            present = [p for p in candidates if inventoried(p)]
            if odoo and not present and count == 1 and count < len(parts):
                # Explicit dependency-root enrollment admits only the Odoo root
                # namespace when both competing canonical files are absent.
                continue
            if len(present) != 1 or present[0] not in files:
                return None, 'missing_competing_or_unparsed_framework_module'
            other = files[present[0]]
            witnesses.append({'path': other.path, 'source_sha256': other.record['sha256'],
                              'source_role': 'framework_package' if count < len(parts) else 'framework_api_source',
                              'partial': other.partial})
            if other.language != 'python' or other.partial or not owned(other.path, dependency):
                return None, 'partial_or_unowned_framework_dependency'
            if count < len(parts):
                if (other.path != candidates[1] or other.module.bindings.get(parts[count]) or
                        any(item['symbol'] == '*' and (finite_parent is None or
                            other.path != finite_parent[0] or item['range']['start_byte'] > finite_parent[1])
                            for item in other.imports)):
                    return None, 'package_initializer_namespace_rebinding_or_missing_package'
        return other, ''

    def api_identity(hint, work, witnesses):
        module, symbol, _, _, reason = hint
        if reason:
            return reason
        other, reason = module_file(module, work, witnesses)
        if reason:
            return reason
        if (module, symbol) in facades:
            target_module, target_symbol = facades[module, symbol]
            bindings = other.module.bindings.get(symbol, [])
            if len(bindings) != 1 or bindings[0].kind != 'import':
                return 'missing_or_shadowed_finite_framework_facade'
            if later_wildcard(other, bindings[0], work, witnesses):
                return 'conditional_or_later_wildcard_framework_export'
            spec = bindings[0].value
            exported = spec['module']
            if exported.startswith('.'):
                parent = _python_parent(other.path, exported)
                stem = None if parent is None else posixpath.join(parent, exported.lstrip('.').replace('.', '/'))
            else:
                stem = prefix + exported.replace('.', '/')
            if stem != prefix + target_module.replace('.', '/') or spec['symbol'] != target_symbol:
                return 'unsupported_framework_reexport'
            ordinal = other.imports.index(spec)
            if other.syntax_metadata['import_contexts'][ordinal]['conditional'] or any(
                    item['symbol'] == '*' and item['range']['start_byte'] > spec['range']['start_byte']
                    for item in other.imports):
                return 'conditional_or_later_wildcard_framework_export'
            witnesses.append(witness(other, spec, 'framework_export'))
            # Only the already validated final explicit facade export may
            # supersede earlier wildcards in this parent package.
            finite_parent = other.path, spec['range']['end_byte']
            module, symbol = target_module, target_symbol
            other, reason = module_file(module, work, witnesses, finite_parent)
            if reason:
                return reason
        bindings = other.module.bindings.get(symbol, [])
        if symbol not in apis.get(module, {}):
            return 'unsupported_framework_api_module'
        if len(bindings) != 1:
            return 'missing_or_ambiguous_framework_api_binding'
        if later_wildcard(other, bindings[0], work, witnesses):
            return 'later_wildcard_framework_api'
        if bindings[0].kind == 'definition':
            definition = definitions[bindings[0].value]
            expected = 'function' if symbol in ('path', 're_path', 'route', 'model') else 'class'
            if definition['kind'] != expected:
                return 'unsupported_framework_api_declaration'
            witnesses.append(witness(other, definition, 'framework_api_definition'))
            return ''
        # Real Django exports path/re_path as functools.partial assignments.
        # That assignment identifies the registration API, never _path targets.
        if module == 'django.urls.conf' and symbol in ('path', 're_path'):
            assignments = [row for row in other.syntax_metadata['python_assignments'] if row['name'] == symbol]
            if len(assignments) != 1 or assignments[0]['conditional'] or assignments[0]['kind'] != 'assignment':
                return 'unsupported_framework_api_assignment'
            row = assignments[0]
            candidate = next((entry for entry in other.candidates if entry[0]['range']['start_byte'] == row['right']['start_byte']
                              and entry[0]['range']['end_byte'] == row['right']['end_byte']), None)
            if candidate is None or candidate[1].type != 'identifier':
                return 'unsupported_framework_api_factory'
            partial = other.module.bindings.get(candidate[1].spelling, [])
            arguments = next(item['arguments'] for item in other.syntax_metadata['calls'] if item['site'] == candidate[0]['id'])
            if (len(partial) != 1 or partial[0].kind != 'import' or
                    (partial[0].value['module'], partial[0].value['symbol']) != ('functools', 'partial') or
                    len(arguments) != 2 or arguments[0]['name'] or arguments[0]['expression']['spelling'] != '_path' or
                    arguments[1]['name'] != 'Pattern' or arguments[1]['expression']['spelling'] != ('RoutePattern' if symbol == 'path' else 'RegexPattern')):
                return 'unsupported_framework_api_factory'
            callback = other.module.bindings.get('_path', [])
            pattern = other.module.bindings.get(arguments[1]['expression']['spelling'], [])
            if len(callback) != 1 or callback[0].kind != 'definition' or len(pattern) != 1 or pattern[0].kind != 'import':
                return 'missing_framework_partial_witness'
            if any(later_wildcard(other, binding, work, witnesses)
                   for binding in (partial[0], callback[0], pattern[0])):
                return 'later_wildcard_framework_partial_witness'
            if pattern[0].value['module'] != '.resolvers' or pattern[0].value['symbol'] != arguments[1]['expression']['spelling']:
                return 'unsupported_framework_pattern_witness'
            witnesses.extend([witness(other, row, 'framework_api_assignment'),
                              witness(other, partial[0].value, 'framework_partial_import'),
                              witness(other, definitions[callback[0].value], 'framework_partial_callback'),
                              witness(other, pattern[0].value, 'framework_pattern_import')])
            return ''
        return 'shadowed_or_unsupported_framework_api'

    def callback_guard(file, expression, scope, work, evidence, seen=None, imported=False):
        work.node()
        seen = set() if seen is None else seen
        if expression.type != 'identifier':
            return 'unsupported_framework_callback_form'
        bindings = scope.lookup(expression.spelling)
        if len(bindings) != 1:
            return 'ambiguous_framework_callback_binding'
        binding = bindings[0]
        key = file.path, binding.scope.ordinal, expression.spelling
        if key in seen:
            return 'framework_callback_alias_cycle'
        seen.add(key)
        if later_wildcard(file, binding, work, evidence):
            return 'later_wildcard_framework_callback'
        if binding.kind == 'alias':
            return callback_guard(file, binding.value, binding.scope, work, evidence, seen, imported)
        if binding.kind == 'import':
            spec = binding.value
            if imported or not spec['module'].startswith('.') or not spec['module'].strip('.') or not spec['symbol'] or spec['symbol'] == '*':
                return 'unsupported_framework_callback_import'
            paths, _, _ = module_paths(file, spec, files, {})
            if len(paths) == 1 and not files[paths[0]].partial:
                other = files[paths[0]]
                return callback_guard(other, Expression('identifier', 0, 0, spec['symbol']), other.module,
                                      work, evidence, seen, True)
        return ''

    def framework_sites(file, work):
        if not owned(file.path, consumer):
            return
        owner = odoo_owner(file.path, enrollment) if odoo else None
        if odoo and (owner is None or any(owner[k] != consumer[k] for k in ('consumer_id', 'service_id'))):
            return
        declarations = {row['id']: row for row in file.syntax_metadata['python_declarations']}
        calls = {row['site']: row['arguments'] for row in file.syntax_metadata['calls']}
        call_metadata = {row['site']: row for row in file.syntax_metadata['calls']}
        candidates = {(entry[0]['range']['start_byte'], entry[0]['range']['end_byte']): entry
                      for entry in file.candidates if entry[0]['role'] == 'call'}
        methods_by_scope = {}
        local_methods = {}
        scopes_by_owner = {scope.owner:scope for scope in file.scopes if scope.owner is not None and scope.kind == 'class'}
        for definition in file.definitions:
            if definition['kind'] == 'method':
                methods_by_scope.setdefault((declarations[definition['id']]['scope'],
                    definition['name'].rsplit('.', 1)[-1]), []).append(definition)
                local_methods.setdefault(declarations[definition['id']]['scope'],[]).append(definition)

        def row(origin, hint, reason, targets, evidence, source_role=None):
            if file.partial:
                reason, source_role = 'partial_framework_origin', 'origin'
            elif source_role is None:
                source_role = next((item['source_role'] for item in evidence if item.get('partial')), None)
            if reason:
                targets = []
            family = 'framework_boundary' if reason else 'framework'
            result = {key: origin[key] for key in ('path', 'range', 'text', 'provenance')}
            result.update(id=f'{file.path}:{origin["range"]["start_byte"]}:{origin["range"]["end_byte"]}:{family}',
                language='python', role=family, family=family,
                relation_kind='unknown_framework_candidate' if reason else hint[2], candidate_relation_kind=hint[2],
                caller=origin.get('caller'), targets=targets, certainty='unresolved' if reason else 'resolved',
                targets_exhaustive=not bool(reason), reason=reason or 'finite source registration; runtime invocation unqualified',
                resolution_method=enrollment['policy_id'], syntax_role='static_framework_registration',
                framework_identity_asserted=not bool(reason), partial=file.partial or source_role is not None,
                partial_source_role=source_role, runtime_qualified=False,
                evidence=evidence)
            if odoo:
                result.update({key: owner[key] for key in ('consumer_id', 'service_id', 'configuration_namespace')})
                result.update(runtime_dispatch='unresolved', runtime_callable_targets=[])
            work.fact(); work.retain(_json_bytes(result, work.budget, work.cancel, measure=True))
            return result

        if odoo:
            def literal(expression):
                if expression['type'] != 'string': return None
                try: value = ast.literal_eval(expression['spelling'])
                except (ValueError, SyntaxError): return None
                return value if type(value) is str and value else None

            def decorator_origin(decorator, method):
                return dict(path=file.path, range=decorator['range'], text=decorator['text'],
                            provenance=method['provenance'], caller=method['id'])

            # A direct imported annotation qualifies a local declaration only.
            # Class inheritance and route invocation are separate unknowns.
            for method in file.definitions:
                work.node()
                declaration = declarations.get(method['id'])
                if method['kind'] != 'method' or declaration is None: continue
                for decorator in declaration['decorators']:
                    hint = import_hint(file, Expression(**decorator['callee']), file.scopes[declaration['scope']], work)
                    if hint is None or hint[2] != 'odoo_route_annotation': continue
                    evidence = [witness(file, hint[3], 'candidate_import'), witness(file, method, 'hook_definition')]
                    class_declaration = next((decl for decl in declarations.values() if decl['id'] == file.scopes[declaration['scope']].owner), None)
                    if class_declaration and class_declaration['header']:
                        evidence.append(witness(file, class_declaration['header'], 'class_header'))
                    evidence.extend(witness(file, item, 'decorator_annotation') for item in declaration['decorators'])
                    evidence.append(witness(file, declaration['decorator_stack'], 'decorator_stack'))
                    evidence.extend(witness(file, item, 'framework_rebinding') for item in file.syntax_metadata['python_assignments']
                        if item['scope'] == 0 and item['name'] == (Expression(**decorator['callee']).base or Expression(**decorator['callee']).spelling))
                    reason = api_identity(hint, work, evidence)
                    value = decorator['value']
                    candidate = candidates.get((value['start_byte'], value['end_byte']))
                    arguments = calls.get(candidate[0]['id'], []) if candidate else []
                    positional = [arg for arg in arguments if not arg['name']]
                    class_scope = file.scopes[declaration['scope']]
                    method_name = method['name'].rsplit('.',1)[-1]
                    if not reason and (len(declaration['decorators']) != 1 or declaration['conditional'] or
                            class_scope.bindings.get(method_name) or len(methods_by_scope.get((class_scope.ordinal,method_name),[])) != 1 or
                            class_declaration and (class_declaration['conditional'] or class_declaration['decorated'])):
                        reason = 'stacked_or_conditional_route_annotation'
                    if not reason:
                        first = positional[0] if len(positional) == 1 else None
                        routes = ([literal(first['expression'])] if first and first['expression']['type'] == 'string' else
                                  [literal(element) for element in first['elements']] if first and first['expression']['type'] in ('list', 'tuple') else [])
                        if not routes or any(route is None for route in routes): reason = 'inherited_computed_or_unsupported_route_annotation'
                    yield row(decorator_origin(decorator, method), hint, reason, [method['id']], evidence)

            # A canonical Model base and one literal local label identify a
            # physical method declaration, never a registry model/call target.
            model_methods = {}
            for definition in file.definitions:
                work.node()
                declaration = declarations.get(definition['id'])
                if definition['kind'] != 'class' or declaration is None: continue
                hints = []
                for base in declaration['bases']:
                    expression = Expression(**base)
                    hint = import_hint(file, expression, file.scopes[declaration['scope']], work)
                    if hint is None and expression.type == 'call':
                        candidate = candidates.get((expression.start_byte,expression.end_byte))
                        hint = import_hint(file,candidate[1],file.scopes[declaration['scope']],work) if candidate and candidate[1] else None
                        if hint: hint = (*hint[:4],'computed_framework_factory_or_member')
                    if hint: hints.append(hint)
                hints = [hint for hint in hints if hint and hint[2] == 'odoo_model_method_declaration' and hint[1] == 'Model']
                if not hints: continue
                hint = hints[0]
                evidence = [witness(file, hint[3], 'candidate_import'), witness(file, declaration['header'], 'class_header')]
                reason = api_identity(hint, work, evidence)
                class_scope = scopes_by_owner[definition['id']]
                labels = [assignment for assignment in file.syntax_metadata['python_assignments'] if
                          assignment['scope'] == class_scope.ordinal and assignment['name'] in ('_name', '_inherit')]
                names = [label for label in labels if label['name'] == '_name']
                labels = names or labels
                if not reason and (declaration['scope'] != 0 or declaration['conditional'] or declaration['decorated'] or
                                   len(declaration['bases']) != 1 or len(file.module.bindings.get(definition['name'], [])) != 1 or
                                   any(assignment['left']['type'] == 'attribute' and assignment['left']['base_identifier'] and
                                       assignment['left']['base'] == definition['name'] for assignment in file.syntax_metadata['python_assignments'])):
                    reason = 'conditional_decorated_or_ambiguous_model_class'
                invalid_label = (len(labels) != 1 or labels[0]['kind'] != 'assignment' or labels[0]['conditional'] or
                                   literal(labels[0]['right']) is None or len(class_scope.bindings.get(labels[0]['name'], [])) != 1)
                if not reason and invalid_label:
                    reason = 'computed_missing_or_ambiguous_model_label'
                evidence.extend(witness(file, label, 'model_label') for label in labels)
                label_boundary = invalid_label and len(labels) == 1
                if label_boundary:
                    origin = dict(path=file.path, range=labels[0]['range'], text=labels[0]['text'],
                                  provenance=definition['provenance'], caller=definition['id'])
                    yield row(origin, hint, reason, [], evidence)
                if file.partial:
                    # A malformed declaration can be an ERROR rather than a
                    # method/call node. Retain its parser-owned source boundary
                    # without recovering a name, callable or nested callsite.
                    covered = -1
                    class_raw = definition['text'].encode()
                    methods = local_methods.get(class_scope.ordinal, [])
                    def in_method(a, b):
                        for method in methods:
                            work.node()
                            if method['range']['start_byte'] <= a < b <= method['range']['end_byte']:
                                return True
                        return False
                    for error in sorted(file.errors, key=lambda item:(item['range']['start_byte'],-item['range']['end_byte'])):
                        work.node()
                        span = error['range']; a,b = span['start_byte'],span['end_byte']
                        if (error['kind'] != 'error' or a == b or a < covered or
                                not class_scope.range['start_byte'] <= a < b <= class_scope.range['end_byte'] or
                                in_method(a,b)):
                            continue
                        covered = b
                        start = definition['range']['start_byte']
                        source = class_raw[a-start:b-start]
                        work.text(len(source))
                        origin = dict(path=file.path,range=span,text=source.decode(),caller=definition['id'],
                            provenance=dict(definition['provenance'],syntax_kind=error['syntax_kind']))
                        yield row(origin,hint,'partial_model_parser_error',[],evidence)
                for method in local_methods.get(class_scope.ordinal,[]):
                    work.node()
                    method_declaration = declarations.get(method['id'])
                    method_reason, method_evidence = reason, list(evidence)
                    decorations = method_declaration['decorators']
                    method_evidence.extend(witness(file, item, 'decorator_annotation') for item in decorations)
                    if decorations and not method_reason:
                        decoration = decorations[0]
                        model_hint = import_hint(file, Expression(**decoration['callee']), class_scope, work)
                        if (len(decorations) != 1 or decoration['value']['type'] != 'attribute' or not model_hint or
                                model_hint[0:2] != ('odoo.api', 'model')):
                            method_reason = 'unsupported_model_method_decorator'
                        else:
                            method_evidence.append(witness(file, model_hint[3], 'model_decorator_import'))
                            method_reason = api_identity(model_hint, work, method_evidence)
                    name = method['name'].rsplit('.', 1)[-1]
                    if not method_reason and (method_declaration['conditional'] or class_scope.bindings.get(name) or
                            len(methods_by_scope.get((class_scope.ordinal, name), [])) != 1):
                        method_reason = 'conditional_or_ambiguous_model_method'
                    method_evidence.append(witness(file, method, 'hook_definition'))
                    if not label_boundary:
                        yield row(dict(method, caller=method['id']), hint, method_reason, [method['id']], method_evidence)
                    model_methods[method['id']] = (hint, method_evidence)
            for origin, expression, scope in candidates.values():
                work.node()
                if (origin.get('caller') in model_methods and expression is not None and expression.type == 'attribute' and
                        call_metadata[origin['id']]['receiver_root']):
                    hint, evidence = model_methods[origin['caller']]
                    hint = (*hint[:2], 'odoo_registry_dispatch', *hint[3:])
                    yield row(origin, hint, 'registry_or_receiver_dispatch_is_unresolved', [], evidence)
            return

        patterns = [assignment for assignment in file.syntax_metadata['python_assignments'] if assignment['scope'] == 0 and assignment['name'] == 'urlpatterns']
        mutation = len(file.module.bindings.get('urlpatterns', [])) != 1 or any(
            assignment['name'] != 'urlpatterns' and any(
                value['type'] == 'identifier' and value['spelling'] == 'urlpatterns'
                for value in (assignment['right'], *assignment['elements']))
            for assignment in file.syntax_metadata['python_assignments'])
        mutation = mutation or any(expression is not None and expression.base_identifier and expression.base == 'urlpatterns'
                                  for _, expression, _ in candidates.values()) or any(
            argument['expression']['type'] == 'identifier' and argument['expression']['spelling'] == 'urlpatterns'
            for arguments in calls.values() for argument in arguments)
        def container_escape(values):
            for value in values:
                work.node()
                if value['spelling'] == 'urlpatterns':
                    return True
            return False
        mutation = mutation or container_escape(value
            for assignment in file.syntax_metadata['python_assignments'] for value in assignment['container_references'])
        mutation = mutation or container_escape(value
            for arguments in calls.values() for argument in arguments for value in argument['container_references'])
        for assignment in patterns:
            elements = assignment['elements'] if assignment['right']['type'] in ('list', 'tuple') else [assignment['right']]
            for element in elements:
                work.check()
                candidate = candidates.get((element['start_byte'], element['end_byte']))
                if candidate is None or candidate[1] is None:
                    continue
                origin, expression, scope = candidate
                hint = import_hint(file, expression, scope, work)
                if hint is None or hint[2] != 'django_route':
                    continue
                evidence = [witness(file, hint[3], 'candidate_import')]
                reason = api_identity(hint, work, evidence)
                targets, source_role = [], None
                arguments = calls[origin['id']]
                if not reason and (mutation or len(patterns) != 1 or assignment['conditional'] or assignment['kind'] != 'assignment'
                                  or assignment['right']['type'] not in ('list', 'tuple')):
                    reason = 'computed_conditional_or_mutated_urlpatterns'
                if not reason:
                    positional = [value['expression'] for value in arguments if not value['name']]
                    literal = None
                    if positional and positional[0]['type'] == 'string':
                        try: literal = ast.literal_eval(positional[0]['spelling'])
                        except (ValueError, SyntaxError): pass
                    if type(literal) is not str or len(positional) < 2:
                        reason = 'computed_route_or_keyword_only_registration'
                    else:
                        callback = Expression(**positional[1])
                        targets, lexical_reason, _ = lexical(file, callback, scope, origin['range']['start_byte'])
                        guard = callback_guard(file, callback, scope, work, evidence)
                        reason = guard or lexical_reason
                        if (len(targets) == 1 and definitions[targets[0]]['kind'] == 'function'
                                and not guard
                                and owned(definitions[targets[0]]['path'], consumer)):
                            reason = ''
                            evidence.append(witness(files[definitions[targets[0]]['path']], definitions[targets[0]], 'callback_definition'))
                        else:
                            targets = []; reason = 'unsupported_or_unresolved_callback: ' + reason
                            name = callback.spelling if callback.type == 'identifier' else callback.base
                            for binding in scope.lookup(name):
                                if binding.kind == 'import':
                                    paths, _, _ = module_paths(file, binding.value, files, {})
                                    if any(files[path].partial for path in paths):
                                        reason, source_role = 'partial_callback_source', 'callback_dependency'
                                        evidence.extend({'path': path, 'source_sha256': files[path].record['sha256'],
                                                         'source_role': source_role, 'partial': True}
                                                        for path in paths if files[path].partial)
                yield row(origin, hint, reason, targets, evidence, source_role)

        for definition in file.definitions:
            work.check()
            declaration = declarations.get(definition['id'])
            if definition['kind'] != 'class' or declaration is None or declaration['scope'] != 0:
                continue
            hints = []
            for base in declaration['bases']:
                expression = Expression(**base)
                hint = import_hint(file, expression, file.module, work)
                if hint is None and expression.type == 'call':
                    candidate = candidates.get((expression.start_byte, expression.end_byte))
                    hint = import_hint(file, candidate[1], file.module, work) if candidate and candidate[1] else None
                    if hint: hint = (*hint[:4], 'computed_framework_factory_or_member')
                if hint: hints.append(hint)
            if not hints:
                continue
            hint = hints[0]
            if hint[2] == 'django_route':
                continue
            if hint[2] == 'django_management_handle' and (definition['name'] != 'Command' or
                    '/management/commands/' not in '/' + file.path):
                continue
            evidence = [witness(file, hint[3], 'candidate_import')]
            reason = api_identity(hint, work, evidence)
            local = file.module.bindings.get(definition['name'], [])
            if not reason and (len(local) != 1 or local[0].kind != 'definition' or declaration['decorated']
                              or declaration['conditional'] or len(declaration['bases']) != 1):
                reason = 'decorated_conditional_multiple_or_shadowed_framework_class'
            method_name = 'handle' if hint[2] == 'django_management_handle' else 'get_queryset'
            class_scope = scopes_by_owner[definition['id']]
            # Methods deliberately have no ordinary lexical binding: class
            # ownership was retained by the collector, without receiver dispatch.
            methods = methods_by_scope.get((class_scope.ordinal, method_name), [])
            target = methods[0] if len(methods) == 1 else None
            if not reason and (target is None or class_scope.bindings.get(method_name) or
                               declarations[target['id']]['decorated'] or declarations[target['id']]['conditional']):
                reason = 'missing_decorated_or_ambiguous_local_hook'
            if target is not None:
                evidence.append(witness(file, target, 'hook_definition'))
            origin = dict(definition, caller=definition['id'])
            yield row(origin, hint, reason, [target['id']] if target and not reason else [], evidence)
        work.check()
    return framework_sites


def _collect_file(record, raw, parser, work, measurements=None):
    """One parser/collector owner; native objects die with this file frame."""
    work.check()
    _record_valid(record, work.budget)
    before = time.perf_counter()
    try:
        tree = parser.parse(raw)
    finally:
        elapsed = time.perf_counter() - before
        work.parse_seconds += elapsed
        if measurements is not None:
            measurements['parse_seconds'] += elapsed
    before = time.perf_counter() if measurements is not None else None
    file = None
    try:
        file = FileFacts(record, raw, tree, work)
        file.collect()
        return file.lower()
    finally:
        if file is not None:
            file.release()
        if measurements is not None:
            measurements['traversal_lowering_seconds'] += time.perf_counter() - before


def collect_file(supplied, budget=None, cancel=None, *, measurements=None, source_measurements=None):
    """Collect one source-only immutable blob for serial or owned worker use.

    Optional sha256/bytes are independent input identity checks, not expected
    facts. Gold keys, configuration blobs and cross-file target lists are not
    accepted. Optional measurements must be an empty dict. Fixed wall timings
    are written there, including failed phases, never into CollectedFile or its
    identity. Backend/parse/traversal-lowering are disjoint; collect_elapsed is
    inclusive and also includes source validation. Handoff encoding is a caller
    stage. No measurements here establish resource defaults or capacity.
    """
    if measurements is not None:
        if type(measurements) is not dict or measurements:
            raise ValueError('Empty native measurement dictionary required')
        measurements.update({key: 0.0 for key in ('backend_setup_seconds',
            'parse_seconds', 'traversal_lowering_seconds', 'collect_elapsed_seconds')})
    if source_measurements is not None and (type(source_measurements) is not dict or source_measurements):
        raise ValueError('Fresh source measurement dictionary required')
    started = time.perf_counter() if measurements is not None else None
    try:
        budget = budget or Budget()
        work = Work(budget, cancel)
        work.check()
        if (type(supplied) is not dict or not {'path', 'language', 'content'} <= set(supplied) or
                set(supplied) - {'path', 'language', 'content', 'kind', 'sha256', 'bytes'}):
            raise ValueError('Source-only blob metadata required')
        raw = supplied['content']
        if type(raw) is not bytes:
            raise ValueError('Source content must be immutable bytes')
        if len(raw) > min(budget.max_file_bytes, budget.max_total_bytes):
            raise StopScan('source_byte_budget_exceeded')
        record = {'path': supplied['path'], 'language': supplied['language'], 'kind': supplied.get('kind', 'source'),
                  'sha256': source_hash(raw, measurements=source_measurements), 'bytes': len(raw)}
        _record_valid(record, budget)
        if ('sha256' in supplied and supplied['sha256'] != record['sha256'] or
                'bytes' in supplied and (type(supplied['bytes']) is not int or supplied['bytes'] != len(raw))):
            raise ValueError('Source identity differs from supplied metadata')
        raw.decode('utf-8')
        before = time.perf_counter() if measurements is not None else None
        try:
            parsers, _ = backend()
        finally:
            if measurements is not None:
                measurements['backend_setup_seconds'] += time.perf_counter() - before
        return _collect_file(record, raw, parsers[record['language']], work, measurements)
    finally:
        if measurements is not None:
            measurements['collect_elapsed_seconds'] = time.perf_counter() - started


def resolve_collected(collected, configurations=None, budget=None, cancel=None, go_context=None, framework_context=None):
    """Bind complete compact files with the same resolver used by extract.

    Admission is aggregate and raises StopScan on limits; per-site resolution
    retains observable partial/error results. Configuration inputs are bounded
    immutable bytes from the caller's guarded source inventory. This function
    does not claim source freshness, query/update qualification or engine choice.
    It never parses source or accepts expected target facts. Optional go_context
    uses declared source controls/package manifests outside the compact record;
    ownership is projected on copies only after the single global resolver.
    """
    work = Work(budget or Budget(), cancel)
    configurations = {} if configurations is None else configurations
    if type(configurations) is not dict:
        raise ValueError('Configuration mapping required')
    files, total = {}, 0
    for path, raw in configurations.items():
        work.check()
        if type(path) is not str or str(PurePosixPath(path)) != path or '\\' in path:
            raise ValueError('Canonical configuration path required')
        SourceRoot.parts(path)
        if type(raw) is not bytes:
            raise ValueError('Immutable configuration bytes required')
        if len(raw) > work.budget.max_file_bytes:
            raise StopScan('source_byte_budget_exceeded')
        raw.decode('utf-8')
        total += len(raw)
        if len(configurations) > work.budget.max_files:
            raise StopScan('file_budget_exceeded')
        if total > work.budget.max_total_bytes:
            raise StopScan('source_byte_budget_exceeded')
    for file in collected:
        work.check()
        if type(file) is not CollectedFile:
            raise ValueError('Node-free CollectedFile required')
        _record_valid(file.record, work.budget)
        if (type(file.counts) is not dict or set(file.counts) != {'nodes', 'definitions'} or
                type(file.counts['nodes']) is not int or file.counts['nodes'] <= 0 or
                type(file.counts['definitions']) is not int or file.counts['definitions'] != len(file.definitions)):
            raise ValueError('Invalid collected admission counts')
        if file.path in files or file.path in configurations:
            raise ValueError('Duplicate source/configuration identity')
        if len(files) + len(configurations) >= work.budget.max_files:
            raise StopScan('file_budget_exceeded')
        total += file.record['bytes']
        if total > work.budget.max_total_bytes:
            raise StopScan('source_byte_budget_exceeded')
        work.nodes += file.counts['nodes']
        work.facts += file.collected_fact_count
        if work.nodes > work.budget.max_nodes:
            raise StopScan('node_budget_exceeded')
        if work.facts > work.budget.max_facts:
            raise StopScan('fact_budget_exceeded')
        work.retain(_json_bytes(file.payload(), work.budget, work.cancel, measure=True))
        files[file.path] = file
    if total > work.budget.max_total_bytes:
        raise StopScan('source_byte_budget_exceeded')
    context = go_contexts(files, configurations, go_context, work) if go_context is not None else None
    framework_context = django_registration_context(framework_context, work)
    resolve = resolver(files, configurations, context, framework_context=framework_context)
    stopped, errors = None, []
    for file in files.values():
        try:
            file.emit_sites(resolve, work)
        except (StopScan, RecursionError) as error:
            stopped = stopped or str(error)
            errors.append({'path': file.path, 'kind': str(error)})
    facts = {'definitions': [d for file in files.values() for d in file.definitions],
             'sites': [s for file in files.values() for s in file.sites]}
    if context is not None:
        facts, _, errors = project_snapshot(facts, [], errors, context['owners'])
    return {'status': 'partial' if stopped or errors or any(f.partial for f in files.values()) else 'complete',
            'facts': facts, 'errors': errors, 'stop_reason': stopped,
            'resources': {'source_bytes': total, 'collected_nodes': work.nodes,
                          'facts_emitted': len(facts['definitions']) + len(facts['sites']),
                          'elapsed_seconds': time.perf_counter() - work.started}}


def extract(blobs, budget=None, cancel=None, go_context=None, framework_context=None):
    """Parse supplied bounded bytes. No expected definitions/cases/targets input.

    Ordinary blobs use path, language, content and optional kind. With an
    explicit go_context, full snapshot metadata and complete non-test package
    inventories are required. Registration is a declared version/revision
    binding; active Go build selection and MVS remain unqualified.
    """
    budget = budget or Budget()
    work = Work(budget, cancel)
    owners = snapshot_blobs(blobs, go_context, work) if go_context is not None else None
    parsers, versions = backend()
    files, configurations, inventory, errors = {}, {}, [], []
    total, stopped, seen = 0, None, set()
    started = time.perf_counter()
    for index, supplied in enumerate(blobs):
        path = supplied['path']
        if path in seen:
            raise ValueError('Inventory paths must be unique')
        seen.add(path)
        raw = supplied['content']
        record = {'path': path, 'language': supplied['language'], 'bytes': len(raw),
                  'sha256': hashlib.sha256(raw).hexdigest(), 'kind': supplied.get('kind', 'source')}
        receipt = dict(record, status='pending')
        inventory.append(receipt)
        try:
            SourceRoot.parts(path)
            if stopped:
                raise StopScan('not_processed_after_' + stopped)
            work.check()
            if index >= budget.max_files:
                raise StopScan('file_budget_exceeded')
            if not isinstance(raw, bytes):
                raise ValueError('Source content must be immutable bytes')
            if len(raw) > budget.max_file_bytes or total + len(raw) > budget.max_total_bytes:
                raise StopScan('source_byte_budget_exceeded')
            total += len(raw)
            if go_context is not None and record['language'] == 'go' and record['kind'] == 'source':
                membership = go_filename_class(owners[path]['physical_path'])
                if membership in ('ignored_filename', 'test_file'):
                    receipt.update(status='go_filename_excluded', reason=membership,
                                   filename_policy=GO_FILENAME_POLICY['version'])
                    continue
            raw.decode('utf-8')
            if record['kind'] == 'configuration':
                configurations[path] = raw
                receipt['status'] = 'configuration'
                continue
            if record['language'] not in parsers:
                receipt['status'] = 'unsupported_language'
                continue
            file = _collect_file(record, raw, parsers[record['language']], work)
            files[path] = file
            receipt['status'] = 'partial_parse' if file.partial else 'parsed'
            receipt['parse_errors'] = file.errors
        except StopScan as error:
            stopped = stopped or str(error)
            receipt['status'] = str(error)
            if path in files:
                del files[path]
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            receipt['status'] = 'source_error'
            receipt['error_kind'] = type(error).__name__
            errors.append({'path': path, 'kind': type(error).__name__})
            files.pop(path, None)
    # Iterable callers and scan no longer leave the last input blob live during
    # binding. A caller-owned input list can still retain its own source bytes.
    raw = supplied = None
    context = None
    if go_context is not None:
        try:
            context = go_contexts(files, configurations, go_context, work)
        except StopScan as error:
            stopped = stopped or str(error)
            context = {'modules': {}, 'packages': {}, 'dependencies': [], 'owners': owners}
    framework_context = django_registration_context(framework_context, work)
    resolve = resolver(files, configurations, context, framework_context=framework_context)
    for file in files.values():
        try:
            file.emit_sites(resolve, work)
        except (StopScan, RecursionError) as error:
            stopped = stopped or str(error)
            errors.append({'path': file.path, 'kind': str(error)})
    facts = {'definitions': [d for file in files.values() for d in file.definitions],
             'sites': [s for file in files.values() for s in file.sites]}
    source_identity = hashlib.sha256('\n'.join(f'{x["path"]}:{x["sha256"]}' for x in sorted(inventory, key=lambda x: x['path'])).encode()).hexdigest()
    if go_context is not None:
        facts, inventory, errors = project_snapshot(facts, inventory, errors, owners)
    return {'schema_version': 1, 'engine': 'tree-sitter', 'rules': RULE_VERSION,
            'versions': versions, 'source_identity': source_identity,
            'status': 'partial' if stopped or errors or any(x['status'] not in ('parsed', 'configuration', 'go_filename_excluded') for x in inventory) else 'complete',
            'inventory': inventory, 'facts': facts, 'errors': errors, 'stop_reason': stopped,
            'resources': {'source_bytes': total, 'nodes_visited': work.nodes,
                          'facts_emitted': len(facts['definitions']) + len(facts['sites']),
                          'parse_seconds': work.parse_seconds, 'elapsed_seconds': time.perf_counter() - started},
            'limits': {'native_parse_cancellation': 'not implemented; bounded input bytes, checks before/after native parse',
                       'binding_scope': 'lexical definitions, guarded local named imports and stable identifier aliases only',
                       **({'go_dependency_policy': 'declared_snapshot_only; explicit registration, ordinary module/require declarations, complete non-test package bytes; active build and MVS unqualified',
                           'go_filename_policy': dict(GO_FILENAME_POLICY)} if go_context is not None else {}),
                       'unknowns': 'computed calls, callback parameters, receiver dispatch, mutable aliases, import/export environments and Go call/conversion ambiguity'}}


def scan(root, paths, budget=None, cancel=None):
    """Read an explicit inventory through SourceRoot, then extract source facts.

    Paths may be strings (language inferred) or metadata-only records. No glob,
    symlink traversal, compiler/provider execution or expected-fact input occurs.
    Every supplied inventory entry gets an observable receipt, including failures.
    """
    budget = budget or Budget()
    root = Path(root)
    paths = list(paths)
    names = [item if isinstance(item, str) else item['path'] for item in paths]
    if len(set(names)) != len(names):
        raise ValueError('Inventory paths must be unique')
    suffixes = {'.py': 'python', '.go': 'go', '.js': 'javascript', '.jsx': 'javascript', '.ts': 'typescript', '.tsx': 'typescript'}
    receipts, total, read_seconds = [], 0, 0.0
    start = time.perf_counter()
    with SourceRoot(root) as source:
        def blobs():
            nonlocal total, read_seconds
            for index, item in enumerate(paths):
                item = {'path': item} if isinstance(item, str) else item
                path = item['path']
                record = {'path': path, 'language': item.get('language', suffixes.get(PurePosixPath(path).suffix, 'unknown')),
                          'kind': item.get('kind', 'configuration' if PurePosixPath(path).name == 'go.mod' else 'source')}
                before = time.perf_counter()
                try:
                    if cancel is not None and cancel():
                        raise StopScan('cancelled')
                    if time.perf_counter() - start > budget.timeout_seconds:
                        raise StopScan('deadline_exceeded')
                    if index >= budget.max_files:
                        raise StopScan('file_budget_exceeded')
                    info = source.info(path)
                    if info.st_size > budget.max_file_bytes or total + info.st_size > budget.max_total_bytes:
                        raise StopScan('source_byte_budget_exceeded')
                    raw, sha, info = source.read(path, budget.max_file_bytes + 1, hash_full=False)
                    if info.st_size != len(raw) or len(raw) > budget.max_file_bytes:
                        raise StopScan('source_byte_budget_exceeded')
                    if ('sha256' in item and item['sha256'] != sha) or ('bytes' in item and item['bytes'] != len(raw)):
                        raise ValueError('Source identity differs from inventory')
                    total += len(raw)
                except (OSError, ValueError, StopScan) as error:
                    receipts.append(dict(record, status=str(error) if isinstance(error, StopScan) else 'source_error',
                                         error_kind=type(error).__name__, errno=getattr(error, 'errno', None)))
                    read_seconds += time.perf_counter() - before
                    continue
                read_seconds += time.perf_counter() - before
                yield dict(record, content=raw)
                raw = None
        result = extract(blobs(), budget, cancel)
    by_path = {x['path']: x for x in result['inventory'] + receipts}
    result['inventory'] = [by_path[item if isinstance(item, str) else item['path']] for item in paths]
    if receipts:
        result['status'] = 'partial'
        result['errors'].extend({'path': x['path'], 'kind': x['status']} for x in receipts)
    result['resources']['read_seconds'] = read_seconds
    return result
