"""Experimental, source-only syntax/direct-binding baseline; never reads gold.

Optional native wheels are confined to the analysis extra. This is deliberately
not a type checker, points-to engine, framework model or runtime call graph.
"""
from dataclasses import dataclass, field
import hashlib
import importlib
from importlib import metadata
import json
import math
from pathlib import Path, PurePosixPath
import posixpath
import time

from repo_graph.source import SourceRoot

PINS = {
    'tree-sitter': '0.26.0', 'tree-sitter-python': '0.25.0',
    'tree-sitter-go': '0.25.0', 'tree-sitter-javascript': '0.25.0',
    'tree-sitter-typescript': '0.23.2',
}
LANGUAGES = ('python', 'go', 'javascript', 'typescript')
RULE_VERSION = 'syntax-direct-v1'
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


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
                   text(raw, node) if node.type == 'identifier' else '', direct,
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
                               kind=scope.kind, name=scope.name, owner=scope.owner, bindings=bindings))
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
            self.sites.append(site)


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
                item['type'] != 'identifier' and item['spelling'] or
                item['type'] == 'identifier' and len(item['spelling'].encode('utf-8')) != item['end_byte'] - item['start_byte']):
            raise ValueError('Invalid compact callee descriptor')
        return Expression(**item)
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
        shape(item, 'name module symbol range')
        string(item['name'])
        string(item['module'])
        if item['symbol'] is not None:
            string(item['symbol'])
        location(item['range'])
        return item['name'], item['module'], item['symbol'], tuple(item['range'][k] for k in ('start_byte', 'end_byte', 'start_line', 'end_line'))
    sequence(payload['imports'], budget.max_nodes)
    import_keys = {import_spec(item) for item in payload['imports']}
    sequence(payload['scopes'], budget.max_nodes + 1)
    if not payload['scopes']:
        raise ValueError('Compact module scope absent')
    scopes = []
    binding_count = 0
    for index, item in enumerate(payload['scopes']):
        shape(item, 'id parent kind name owner bindings')
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
                      item['owner'], ordinal=index)
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
    shape(syntax, 'package_clauses go_control_directive go_bodyless_function go_cgo_import')
    sequence(syntax['package_clauses'], budget.max_nodes)
    for item in syntax['package_clauses']:
        shape(item, 'name range')
        string(item['name'])
        location(item['range'])
    for key in ('go_control_directive', 'go_bodyless_function', 'go_cgo_import'):
        boolean(syntax[key])
    if record['language'] != 'go' and (syntax['package_clauses'] or any(syntax[k] for k in syntax if k != 'package_clauses')):
        raise ValueError('Foreign compact language metadata')
    work.check()
    return CollectedFile(dict(record), scopes, definitions, candidates, payload['imports'],
                         payload['errors'], payload['partial'], dict(payload['counts']), syntax,
                         collector_sha256=payload['collector_sha256'])


class FileFacts:
    def __init__(self, record, raw, tree, work):
        self.record, self.raw, self.tree, self.work = record, raw, tree, work
        self.path, self.language = record['path'], record['language']
        self.module = Scope(None, 'module', '')
        self.scopes = [self.module]
        self.nodes = []
        self.definitions, self.sites, self.imports, self.errors = [], [], [], []
        self.callable_nodes = {}
        self.partial = tree.root_node.has_error
        self.initial_counts = work.nodes, work.facts
        self.syntax_metadata = {'package_clauses': [], 'go_control_directive': False,
                                'go_bodyless_function': False, 'go_cgo_import': False}

    def new_scope(self, parent, kind, name, owner=None):
        scope = Scope(parent, kind, name, owner, ordinal=len(self.scopes))
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
                lexical_parent = scope.parent if scope.kind == 'class' else scope
                child_scope = self.new_scope(lexical_parent, 'function', definition['name'], definition['id'])
                self.parameters(node, child_scope)
            elif typ in ('class_definition', 'class_declaration', 'interface_declaration'):
                name = text(self.raw, node.child_by_field_name('name'))
                kind = 'interface' if typ == 'interface_declaration' else 'class'
                definition = self.add_definition(node, name, kind, scope)
                child_scope = self.new_scope(scope, 'class', definition['name'], definition['id'])
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
                                    owner['id'] if owner else scope.owner)
                self.parameters(node, child_scope)
            elif typ in ('statement_block', 'block') and self.language != 'python':
                child_scope = self.new_scope(scope, 'block', scope.name, scope.owner)
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
                       'assignment_statement', 'variable_declarator', 'var_spec', 'update_expression'):
            return
        left = node.child_by_field_name('left') or node.child_by_field_name('name') or node.child_by_field_name('argument')
        right = unwrap(node.child_by_field_name('right') or node.child_by_field_name('value'))
        names = identifiers(left)
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
        if scope.kind != 'module':
            # Local imports are left unknown rather than misclassified as globals.
            if node.type in ('import_statement', 'import_from_statement') and self.language == 'python':
                for child in node.named_children:
                    if child.type == 'aliased_import':
                        alias = child.child_by_field_name('alias') or child.child_by_field_name('name')
                        scope.add(text(self.raw, alias), Binding('unknown', 'local import unsupported', alias, scope))
                    elif child.type == 'dotted_name' and child != node.child_by_field_name('module_name'):
                        scope.add(text(self.raw, child).split('.')[0], Binding('unknown', 'local import unsupported', child, scope))
            return
        specs = []
        if self.language == 'python' and node.type == 'import_statement':
            for child in node.named_children:
                name = child.child_by_field_name('alias') if child.type == 'aliased_import' else child
                scope.add(text(self.raw, name).split('.')[0], Binding('unknown', 'absolute Python import environment is not modeled', name, scope))
        if self.language == 'python' and node.type == 'import_from_statement':
            module = text(self.raw, node.child_by_field_name('module_name'))
            for child in node.named_children:
                if child.type == 'aliased_import':
                    name = text(self.raw, child.child_by_field_name('name'))
                    alias_node = child.child_by_field_name('alias') or child.child_by_field_name('name')
                    specs.append((text(self.raw, alias_node), module, name, alias_node))
                elif child.type == 'dotted_name' and child != node.child_by_field_name('module_name'):
                    specs.append((text(self.raw, child), module, text(self.raw, child), child))
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
        elif self.language == 'go' and node.type == 'import_spec':
            module = text(self.raw, node.child_by_field_name('path'))[1:-1]
            alias = node.child_by_field_name('name')
            specs.append((text(self.raw, alias) if alias else module.rsplit('/', 1)[-1], module, None, alias or node))
        for name, module, symbol, binding_node in specs:
            item = {'name': name, 'module': module, 'symbol': symbol, 'range': self.location(node)}
            self.imports.append(item)
            if self.language == 'go' and module == 'C':
                self.syntax_metadata['go_cgo_import'] = True
            conditional = self.language == 'python' and self.conditional(node)
            scope.add(name, Binding('unknown' if conditional else 'import',
                                    'conditional import is not flow-resolved' if conditional else item,
                                    binding_node, scope, True))

    def lower(self):
        """Finish syntax-dependent decisions while this one file's Tree lives."""
        scopes = []
        for scope in self.scopes:
            self.work.check()
            scopes.append(Scope(scopes[scope.parent.ordinal] if scope.parent else None,
                                scope.kind, scope.name, scope.owner, ordinal=scope.ordinal))
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
        counts = {'nodes': self.work.nodes - self.initial_counts[0],
                  'definitions': self.work.facts - self.initial_counts[1]}
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


def module_paths(file, spec, files, configurations):
    module, symbol = spec['module'], spec['symbol']
    parent = posixpath.dirname(file.path)
    if file.language == 'python':
        if not module.startswith('.'):
            return [], symbol, 'absolute Python import environment is not modeled'
        count = len(module) - len(module.lstrip('.'))
        for _ in range(count - 1):
            parent = posixpath.dirname(parent)
        stem = posixpath.join(parent, module[count:].replace('.', '/'))
        paths = [stem + '.py', posixpath.join(stem, '__init__.py')]
    elif file.language in ('javascript', 'typescript'):
        if not module.startswith('.'):
            return [], symbol, 'package import/export resolution is not modeled'
        stem = posixpath.normpath(posixpath.join(parent, module))
        paths = [stem] if PurePosixPath(stem).suffix else [stem + ext for ext in ('.ts', '.tsx', '.js', '.jsx')] + [posixpath.join(stem, 'index' + ext) for ext in ('.ts', '.js')]
    else:
        paths = []
        for config_path, content in configurations.items():
            for line in content.decode('utf-8').splitlines():
                if line.startswith('module '):
                    prefix = line.split()[1]
                    if module == prefix or module.startswith(prefix + '/'):
                        directory = posixpath.normpath(posixpath.join(posixpath.dirname(config_path), module[len(prefix):].lstrip('/')))
                        paths.extend(path for path in files if posixpath.dirname(path) == directory and path.endswith('.go'))
    found = sorted(set(path for path in paths if path in files and files[path].language == file.language))
    return found, symbol, '' if found else 'import target absent from guarded source inventory'


def resolver(files, configurations):
    definitions = {definition['id']: definition for file in files.values() for definition in file.definitions}

    def imported(file, item, member):
        paths, symbol, reason = module_paths(file, item, files, configurations)
        # Export presence cannot choose an import module. An extension/search
        # policy must first identify one module independently of its symbols.
        # Go is the exception: one package directory can contain many files.
        if file.language != 'go' and len(paths) > 1:
            return [], 'multiple inventoried module paths; extension/package selection policy is not modeled', 'import_alias'
        if file.language == 'go' and len({posixpath.dirname(path) for path in paths}) > 1:
            return [], 'multiple package directories match module configurations', 'import_alias'
        symbol = member if symbol is None else symbol
        if not symbol:
            return [], 'module value has no callable target', 'import_alias'
        targets = []
        for path in paths:
            other = files[path]
            bindings = other.module.bindings.get(symbol, [])
            # This baseline resolves only one ordinary source definition, not
            # reexports, conditional exports, overloads or partial files.
            if not other.partial and len(bindings) == 1 and bindings[0].kind == 'definition':
                target = definitions[bindings[0].value]
                exported = other.language not in ('javascript', 'typescript') or target['text'].startswith('export ')
                go_exported = other.language != 'go' or symbol[0].isupper()
                if target['callable'] and exported and go_exported:
                    targets.append(target['id'])
        if len(targets) == 1:
            return targets, 'one source definition in an inventoried local module', 'import_alias'
        return [], reason or 'import target is missing, partial, ambiguous or unsupported', 'import_alias'

    def resolve(file, node, scope, offset, seen=None):
        seen = set() if seen is None else seen
        if node is None:
            return [], 'callee unavailable', 'unknown'
        if node.type in ('attribute', 'member_expression', 'selector_expression'):
            if node.base_identifier:
                bindings = scope.lookup(node.base)
                if len(bindings) == 1 and bindings[0].kind == 'import' and bindings[0].value['symbol'] is None:
                    return imported(file, bindings[0].value, node.member)
            return [], 'receiver dispatch requires type/points-to analysis; candidates not enumerated', 'unsupported_receiver'
        if node.type != 'identifier':
            return [], 'computed or unsupported callee; possible targets not enumerated', 'unsupported_dynamic'
        name = node.spelling
        bindings = scope.lookup(name)
        if len(bindings) != 1:
            return [], 'binding absent or multiply assigned in this lexical scope', 'lexical_unknown'
        binding = bindings[0]
        key = (id(binding.scope), name)
        if key in seen:
            return [], 'alias cycle; no exact target', 'alias_cycle'
        if not binding.hoisted and binding.node.start_byte > offset and binding.scope.kind != 'module':
            return [], 'use before a local declaration is not resolved', 'declaration_order'
        if binding.kind == 'definition':
            target = definitions[binding.value]
            if not target['callable']:
                return [], 'value is a type/interface declaration, not an identified callable', 'lexical_unknown'
            return [target['id']], 'one lexically visible source definition', 'lexical_direct'
        if binding.kind == 'import':
            return imported(file, binding.value, None)
        if binding.kind == 'alias':
            seen.add(key)
            targets, reason, _ = resolve(file, binding.value, binding.scope, binding.node.start_byte, seen)
            return targets, reason, 'stable_value_alias'
        return [], str(binding.value), 'lexical_unknown'
    return resolve


def _collect_file(record, raw, parser, work):
    """One parser/collector owner; native objects die with this file frame."""
    work.check()
    _record_valid(record, work.budget)
    before = time.perf_counter()
    tree = parser.parse(raw)
    work.parse_seconds += time.perf_counter() - before
    file = FileFacts(record, raw, tree, work)
    try:
        file.collect()
        return file.lower()
    finally:
        file.release()


def collect_file(supplied, budget=None, cancel=None):
    """Collect one source-only immutable blob for serial or owned worker use.

    Optional sha256/bytes are independent input identity checks, not expected
    facts. Gold keys, configuration blobs and cross-file target lists are not
    accepted. Timing is observed by the caller; counts describe collected work.
    """
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
              'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    _record_valid(record, budget)
    if ('sha256' in supplied and supplied['sha256'] != record['sha256'] or
            'bytes' in supplied and (type(supplied['bytes']) is not int or supplied['bytes'] != len(raw))):
        raise ValueError('Source identity differs from supplied metadata')
    raw.decode('utf-8')
    parsers, _ = backend()
    return _collect_file(record, raw, parsers[record['language']], work)


def resolve_collected(collected, configurations=None, budget=None, cancel=None):
    """Bind complete compact files with the same resolver used by extract.

    Admission is aggregate and raises StopScan on limits; per-site resolution
    retains observable partial/error results. Configuration inputs are bounded
    immutable bytes from the caller's guarded source inventory. This function
    does not claim source freshness, query/update qualification or engine choice.
    It never parses source or accepts expected target facts.
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
        work.facts += file.counts['definitions']
        if work.nodes > work.budget.max_nodes:
            raise StopScan('node_budget_exceeded')
        if work.facts > work.budget.max_facts:
            raise StopScan('fact_budget_exceeded')
        work.retain(_json_bytes(file.payload(), work.budget, work.cancel, measure=True))
        files[file.path] = file
    if total > work.budget.max_total_bytes:
        raise StopScan('source_byte_budget_exceeded')
    resolve = resolver(files, configurations)
    stopped, errors = None, []
    for file in files.values():
        try:
            file.emit_sites(resolve, work)
        except (StopScan, RecursionError) as error:
            stopped = stopped or str(error)
            errors.append({'path': file.path, 'kind': str(error)})
    facts = {'definitions': [d for file in files.values() for d in file.definitions],
             'sites': [s for file in files.values() for s in file.sites]}
    return {'status': 'partial' if stopped or errors or any(f.partial for f in files.values()) else 'complete',
            'facts': facts, 'errors': errors, 'stop_reason': stopped,
            'resources': {'source_bytes': total, 'collected_nodes': work.nodes,
                          'facts_emitted': len(facts['definitions']) + len(facts['sites']),
                          'elapsed_seconds': time.perf_counter() - work.started}}


def extract(blobs, budget=None, cancel=None):
    """Parse supplied bounded bytes. No expected definitions/cases/targets input.

    blob records contain only path, language, content and optional kind. This
    entrypoint is useful for immutable snapshots and update comparisons.
    """
    budget = budget or Budget()
    work = Work(budget, cancel)
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
    resolve = resolver(files, configurations)
    for file in files.values():
        try:
            file.emit_sites(resolve, work)
        except (StopScan, RecursionError) as error:
            stopped = stopped or str(error)
            errors.append({'path': file.path, 'kind': str(error)})
    facts = {'definitions': [d for file in files.values() for d in file.definitions],
             'sites': [s for file in files.values() for s in file.sites]}
    source_identity = hashlib.sha256('\n'.join(f'{x["path"]}:{x["sha256"]}' for x in sorted(inventory, key=lambda x: x['path'])).encode()).hexdigest()
    return {'schema_version': 1, 'engine': 'tree-sitter', 'rules': RULE_VERSION,
            'versions': versions, 'source_identity': source_identity,
            'status': 'partial' if stopped or errors or any(x['status'] not in ('parsed', 'configuration') for x in inventory) else 'complete',
            'inventory': inventory, 'facts': facts, 'errors': errors, 'stop_reason': stopped,
            'resources': {'source_bytes': total, 'nodes_visited': work.nodes,
                          'facts_emitted': len(facts['definitions']) + len(facts['sites']),
                          'parse_seconds': work.parse_seconds, 'elapsed_seconds': time.perf_counter() - started},
            'limits': {'native_parse_cancellation': 'not implemented; bounded input bytes, checks before/after native parse',
                       'binding_scope': 'lexical definitions, guarded local named imports and stable identifier aliases only',
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
