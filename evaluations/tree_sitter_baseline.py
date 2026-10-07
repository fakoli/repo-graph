"""Experimental, source-only syntax/direct-binding baseline; never reads gold.

Optional native wheels are confined to the analysis extra. This is deliberately
not a type checker, points-to engine, framework model or runtime call graph.
"""
from dataclasses import dataclass, field
import hashlib
import importlib
from importlib import metadata
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

    def __post_init__(self):
        if min(self.max_files, self.max_file_bytes, self.max_total_bytes,
               self.max_nodes, self.max_facts, self.timeout_seconds) <= 0:
            raise ValueError('Budgets must be positive')


class StopScan(RuntimeError):
    pass


class Work:
    def __init__(self, budget, cancel):
        self.budget, self.cancel = budget, cancel
        self.started, self.nodes, self.facts = time.perf_counter(), 0, 0

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

    def add(self, name, binding):
        self.bindings.setdefault(name, []).append(binding)

    def lookup(self, name):
        scope = self
        while scope:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return []


class FileFacts:
    def __init__(self, record, raw, tree, work):
        self.record, self.raw, self.tree, self.work = record, raw, tree, work
        self.path, self.language = record['path'], record['language']
        self.module = Scope(None, 'module', '')
        self.nodes = []
        self.definitions, self.sites, self.imports, self.errors = [], [], [], []
        self.callable_nodes = {}
        self.partial = tree.root_node.has_error

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
                child_scope = Scope(lexical_parent, 'function', definition['name'], definition['id'])
                self.parameters(node, child_scope)
            elif typ in ('class_definition', 'class_declaration', 'interface_declaration'):
                name = text(self.raw, node.child_by_field_name('name'))
                kind = 'interface' if typ == 'interface_declaration' else 'class'
                definition = self.add_definition(node, name, kind, scope)
                child_scope = Scope(scope, 'class', definition['name'], definition['id'])
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
                child_scope = Scope(scope, 'function', owner['name'] if owner else scope.name,
                                    owner['id'] if owner else scope.owner)
                self.parameters(node, child_scope)
            elif typ in ('statement_block', 'block') and self.language != 'python':
                child_scope = Scope(scope, 'block', scope.name, scope.owner)
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
            conditional = self.language == 'python' and self.conditional(node)
            scope.add(name, Binding('unknown' if conditional else 'import',
                                    'conditional import is not flow-resolved' if conditional else item,
                                    binding_node, scope, True))

    def emit_sites(self, resolve):
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
            targets, reason, method = resolve(self, callee, scope, node.start_byte)
            if syntax_role == 'call_or_conversion':
                targets, reason, method = [], 'Go grammar cannot distinguish this call from a type conversion without type information', 'unsupported_go_ambiguity'
            if self.partial:
                targets, reason, method = [], 'file contains parse errors; bindings are withheld', 'partial_parse'
            # Only resolved callable references are claimed; unknown references remain
            # visible in these selected value contexts, with an explicit reason.
            self.work.fact()
            self.sites.append({'id': f'{self.path}:{node.start_byte}:{node.end_byte}:{role}',
                               'path': self.path, 'language': self.language, 'role': role,
                               'range': self.location(node), 'text': text(self.raw, node),
                               'caller': scope.owner, 'targets': targets,
                               'certainty': 'resolved' if targets else 'unresolved',
                               'targets_exhaustive': bool(targets), 'reason': reason,
                               'resolution_method': method, 'syntax_role': syntax_role,
                               'provenance': self.provenance(node.type)})


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
            base = node.child_by_field_name('object') or node.child_by_field_name('operand')
            member = node.child_by_field_name('attribute') or node.child_by_field_name('property') or node.child_by_field_name('field')
            if base is not None and base.type == 'identifier':
                bindings = scope.lookup(text(file.raw, base))
                if len(bindings) == 1 and bindings[0].kind == 'import' and bindings[0].value['symbol'] is None:
                    return imported(file, bindings[0].value, text(file.raw, member))
            return [], 'receiver dispatch requires type/points-to analysis; candidates not enumerated', 'unsupported_receiver'
        if node.type != 'identifier':
            return [], 'computed or unsupported callee; possible targets not enumerated', 'unsupported_dynamic'
        name = text(file.raw, node)
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


def extract(blobs, budget=None, cancel=None):
    """Parse supplied bounded bytes. No expected definitions/cases/targets input.

    blob records contain only path, language, content and optional kind. This
    entrypoint is useful for immutable snapshots and update comparisons.
    """
    budget = budget or Budget()
    work = Work(budget, cancel)
    parsers, versions = backend()
    files, configurations, inventory, errors = {}, {}, [], []
    total, stopped = 0, None
    started = time.perf_counter()
    parse_seconds = 0.0
    for index, supplied in enumerate(blobs):
        path = supplied['path']
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
            before = time.perf_counter()
            tree = parsers[record['language']].parse(raw)
            parse_seconds += time.perf_counter() - before
            file = FileFacts(record, raw, tree, work)
            files[path] = file
            file.collect()
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
    resolve = resolver(files, configurations)
    for file in files.values():
        try:
            file.emit_sites(resolve)
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
                          'parse_seconds': parse_seconds, 'elapsed_seconds': time.perf_counter() - started},
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
    blobs, receipts, total = [], [], 0
    start = time.perf_counter()
    with SourceRoot(root) as source:
        for index, item in enumerate(paths):
            item = {'path': item} if isinstance(item, str) else item
            path = item['path']
            record = {'path': path, 'language': item.get('language', suffixes.get(PurePosixPath(path).suffix, 'unknown')),
                      'kind': item.get('kind', 'configuration' if PurePosixPath(path).name == 'go.mod' else 'source')}
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
                blobs.append(dict(record, content=raw))
            except (OSError, ValueError, StopScan) as error:
                receipts.append(dict(record, status=str(error) if isinstance(error, StopScan) else 'source_error',
                                     error_kind=type(error).__name__, errno=getattr(error, 'errno', None)))
    result = extract(blobs, budget, cancel)
    by_path = {x['path']: x for x in result['inventory'] + receipts}
    result['inventory'] = [by_path[item if isinstance(item, str) else item['path']] for item in paths]
    if receipts:
        result['status'] = 'partial'
        result['errors'].extend({'path': x['path'], 'kind': x['status']} for x in receipts)
    result['resources']['read_seconds'] = time.perf_counter() - start - result['resources']['elapsed_seconds']
    return result
