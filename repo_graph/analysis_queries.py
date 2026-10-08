"""Bounded occurrence queries over captured structural facts.

SQLSnapshot reads the shared persisted index. Snapshot is a finite comparison
adapter for evaluations; it is never constructed by the production index.
"""
from collections import deque
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from pathlib import Path
from pathlib import PurePosixPath
from threading import Lock

from .search import connect, SNAPSHOT_LOCK, _artifact_token
from .source import SourceRoot

QUERY_RULE_VERSION = 'physical-occurrence-v2'
IMPACT_RULE_VERSION = 'captured-impact-v1'
_LOADED_QUERY_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def code_identity():
    observed = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if observed != _LOADED_QUERY_SHA256:
        raise RuntimeError('Query implementation changed since module import')
    return observed


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def validate_impact_receipt(receipt, identities):
    """Pure captured identity check shared by queries, status and source inspection."""
    if (type(receipt) is not dict or receipt.get('schema') != IMPACT_RULE_VERSION or
            any(receipt.get(k) != v for k, v in identities.items()) or
            type(receipt.get('git_change')) is not dict or type(receipt.get('revision_dirty')) is not dict or
            receipt.get('contracts_available') is not False or
            receipt.get('historical_call_closure') != 'unavailable_current_index_only' or
            len(encoded(receipt)) > 32768 or
            receipt.get('identity') != hashlib.sha256(encoded({k: v for k, v in receipt.items() if k != 'identity'})).hexdigest()):
        raise ValueError('Missing, stale or foreign impact projection; refresh the index')
    return receipt


def _row_entities(row):
    """Declaration handles consume entities; physical occurrences consume edges."""
    if 'site' not in row:
        return {row['id']}
    entities = {handle['id'] for handle in (row['caller'], row['target']) if handle is not None}
    entities.update(handle['id'] for handle in row.get('evidence', []) if handle.get('source_role') in (
        'framework_api_definition', 'framework_partial_callback', 'callback_definition', 'hook_definition'))
    return entities


@dataclass(frozen=True)
class Limits:
    max_entities: int = 50
    max_edges: int = 100
    max_examined_relationships: int = 10000
    max_response_bytes: int = 32768
    max_excerpt_bytes: int = 8192
    timeout_seconds: float = 0.5

    def __post_init__(self):
        for field, ceiling in (('max_entities', 256), ('max_edges', 256),
                               ('max_examined_relationships', 100000), ('max_response_bytes', 1048576),
                               ('max_excerpt_bytes', 65536)):
            value = getattr(self, field)
            minimum = 0 if field == 'max_excerpt_bytes' else 1
            if type(value) is not int or not minimum <= value <= ceiling:
                raise ValueError('Invalid query limit: ' + field)
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 30:
            raise ValueError('Invalid query deadline')


def _impact_options(selector, relations, certainties):
    if selector is not None:
        if type(selector) is not dict or selector.get('kind') not in ('source_area', 'git_change'):
            raise ValueError('Invalid impact selector')
        if selector['kind'] == 'git_change':
            base = selector.get('base_revision')
            if set(selector) != {'kind', 'base_revision'} or type(base) is not str or len(base) not in (40, 64) or any(c not in '0123456789abcdef' for c in base):
                raise ValueError('Exact captured Git base required')
            selector = dict(selector)
        else:
            paths = selector.get('paths')
            if set(selector) != {'kind', 'paths'} or type(paths) is not list or not 1 <= len(paths) <= 50:
                raise ValueError('Bounded source-area paths required')
            for path in paths:
                if type(path) is not str or not path or len(path.encode('utf-8')) > 4096 or '\\' in path or '\0' in path:
                    raise ValueError('Canonical source-area path required')
                if path != '.':
                    value = path[:-1] if path.endswith('/') else path
                    if str(PurePosixPath(value)) != value:
                        raise ValueError('Canonical source-area path required')
                    try: SourceRoot.parts(value)
                    except OSError as error: raise ValueError('Canonical source-area path required') from error
            selector = {'kind': 'source_area', 'paths': sorted(set(paths))}
            if len(encoded(selector)) > 8192:
                raise ValueError('Source-area selector byte ceiling exhausted')
    def choices(value, allowed, default):
        if value is None: return default
        if type(value) is not list or not value or len(value) > len(allowed) or any(type(v) is not str or v not in allowed for v in value) or len(set(value)) != len(value):
            raise ValueError('Invalid impact filters')
        return sorted(value)
    return selector, choices(relations, ('call', 'import'), ['call', 'import'] if selector else ['call']), choices(
        certainties, ('resolved', 'candidate', 'unresolved'), ['candidate', 'resolved', 'unresolved'])


def _framework_filters(operation, families, kinds):
    def choices(value, allowed):
        if value is None: return sorted(allowed)
        if type(value) is not list or not value or len(value) > len(allowed) or any(type(v) is not str or v not in allowed for v in value) or len(set(value)) != len(value):
            raise ValueError('Invalid framework family/kind filter')
        return sorted(value)
    if operation != 'framework':
        if families is not None or kinds is not None: raise ValueError('Framework filters require framework operation')
        return None, None
    return choices(families, ('framework', 'framework_boundary')), choices(kinds, (
        'django_route', 'django_management_handle', 'django_orm_get_queryset', 'unknown_framework_candidate'))


def _validate_query(seed, operation, depth, prefix, scope, role, cancel, selector=None, relations=None, certainties=None):
    if (operation not in ('symbol', 'reference', 'call', 'framework', 'callees', 'callers', 'reachable', 'impact') or
            type(depth) is not int or not 1 <= depth <= 32 or role not in ('call', 'reference', 'all') or
            any(type(v) is not str or len(v) > 4096 for v in (prefix, scope)) or
            seed is not None and (type(seed) is not str or not seed or len(seed) > 8192) or
            seed is None and operation in ('callees', 'callers', 'reachable', 'impact') and selector is None or
            cancel is not None and not callable(cancel)):
        raise ValueError('Invalid bounded persisted query')
    if selector is not None or relations is not None or certainties is not None:
        if operation != 'impact' or selector is not None and seed is not None:
            raise ValueError('Selectors and filters belong only to impact')
        _impact_options(selector, relations, certainties)


class Snapshot:
    """Finite comparison snapshot; continuations expire and bind every filter.

    ponytail: in-memory component snapshot capped at 64 MiB/40000 facts;
    use SQLSnapshot for persisted queries instead of this comparison adapter.
    Cursor state is capped at 64 KiB each, 32 live continuations and 60 seconds.
    """
    def __init__(self, facts, source_identity, analyzer_identity, *, clock=time.monotonic):
        for identity in (source_identity, analyzer_identity):
            if type(identity) is not str or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
                raise ValueError('Snapshot identity must be a SHA256 digest')
        if type(facts) is not dict or set(facts) != {'definitions', 'sites'} or any(type(v) is not list for v in facts.values()) or sum(map(len, facts.values())) > 40000:
            raise ValueError('Invalid or unbounded facts')
        # Stop encoding at the byte cap; do not serialize an unbounded input first.
        parts, size = [], 0
        for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(facts):
            raw = part.encode(); size += len(raw)
            if size > 64 * 1024 * 1024:
                raise ValueError('Snapshot byte budget exceeded')
            parts.append(raw)
        copied = json.loads(b''.join(parts)); del parts
        for item in copied['definitions'] + copied['sites']:
            if type(item) is not dict or any(type(item[key]) is not str for key in ('id', 'path')):
                raise ValueError('Invalid source handle primitives')
            source_sha = item['provenance']['source_sha256']
            if type(source_sha) is not str or len(source_sha) != 64 or any(c not in '0123456789abcdef' for c in source_sha):
                raise ValueError('Invalid source evidence digest')
        for item in copied['definitions']:
            if type(item['name']) is not str:
                raise ValueError('Invalid declaration name')
        for item in copied['sites']:
            if (type(item['targets_exhaustive']) is not bool or type(item.get('reason', '')) is not str or
                    item['caller'] is not None and type(item['caller']) is not str or
                    type(item['targets']) is not list or any(type(target) is not str for target in item['targets'])):
                raise ValueError('Invalid relationship primitives')
        self._definitions = {item['id']: item for item in copied['definitions']}
        self._sites = {item['id']: item for item in copied['sites']}
        if len(self._definitions) != len(copied['definitions']) or len(self._sites) != len(copied['sites']):
            raise ValueError('Duplicate structural identity')
        self._outgoing, self._incoming = {}, {}
        for site in self._sites.values():
            if (site['caller'] is not None and site['caller'] not in self._definitions or
                    type(site['targets']) is not list or len(site['targets']) != len(set(site['targets'])) or
                    any(target not in self._definitions for target in site['targets'])):
                raise ValueError('Dangling structural relationship')
            if site['role'] not in ('call', 'reference', 'framework', 'framework_boundary') or site['certainty'] not in ('resolved', 'candidate', 'unresolved'):
                raise ValueError('Unknown structural role/certainty')
            for target in site['targets'] or [None]:
                key = (site['id'], target)
                self._outgoing.setdefault(site['caller'], []).append(key)
                if target is not None:
                    self._incoming.setdefault(target, []).append(key)
        for item in (*self._definitions.values(), *self._sites.values()):
            span = item['range']
            if (type(item['id']) is not str or len(item['id']) > 8192 or type(item['path']) is not str or len(item['path']) > 4096 or
                    set(span) != {'start_byte', 'end_byte', 'start_line', 'end_line'} or
                    any(type(v) is not int for v in span.values()) or
                    not 0 <= span['start_byte'] <= span['end_byte'] or not 1 <= span['start_line'] <= span['end_line']):
                raise ValueError('Invalid source handle')
        for adjacency in (self._outgoing, self._incoming):
            for relations in adjacency.values():
                relations.sort(key=self._order)
        self._clock, self._secret, self._continuations = clock, secrets.token_bytes(32), {}
        self._source_identity, self._analyzer_identity = source_identity, analyzer_identity
        self._generation = hashlib.sha256(encoded({'source': source_identity, 'analyzer': analyzer_identity, 'facts': copied})).hexdigest()

    @property
    def generation(self):
        return self._generation

    @property
    def source_identity(self):
        return self._source_identity

    @property
    def analyzer_identity(self):
        return self._analyzer_identity

    def _order(self, key):
        site, target = self._sites[key[0]], self._definitions.get(key[1])
        span = site['range']; target_span = target['range'] if target else {}
        return (site['path'], span['start_byte'], span['end_byte'], site['role'],
                target['path'] if target else '', target_span.get('start_byte', -1), target_span.get('end_byte', -1),
                key[0], key[1] or '')

    def _handle(self, identifier):
        if identifier is None:
            return None
        item = self._definitions[identifier]
        return {'id': identifier, 'path': item['path'], 'range': dict(item['range']),
                'name': item['name'][:256], 'name_truncated': len(item['name']) > 256,
                'source_sha256': item['provenance']['source_sha256']}

    def _row(self, key):
        site, target = self._sites[key[0]], key[1]
        return {'site': {'id': site['id'], 'path': site['path'], 'range': dict(site['range']),
                         'role': site['role'], 'source_sha256': site['provenance']['source_sha256']},
                'caller': self._handle(site['caller']), 'target': self._handle(target),
                'certainty': site['certainty'], 'targets_exhaustive': site['targets_exhaustive'],
                'reason': site.get('reason', '')[:256], 'reason_truncated': len(site.get('reason', '')) > 256}

    def _cursor(self, state, signature):
        payload = encoded(state)
        if len(payload) > 65536:
            raise ValueError('Continuation state budget exceeded')
        token = hmac.new(self._secret, signature.encode() + payload, hashlib.sha256).hexdigest()
        now = self._clock()
        self._continuations = {key: value for key, value in self._continuations.items() if value[0] > now}
        if token not in self._continuations and len(self._continuations) >= 32:
            raise ValueError('Live continuation capacity exhausted')
        self._continuations.setdefault(token, (now + 60, signature, payload))
        return token

    def query(self, seed, *, operation='callees', depth=1, prefix='', scope='', role='call',
              limits=None, cursor=None, cancel=None):
        """Page occurrence relations; rejected filters consume examined work."""
        limits = limits or Limits()
        if (seed not in self._definitions or operation not in ('callees', 'callers', 'reachable', 'impact') or
                type(depth) is not int or not 1 <= depth <= 32 or role not in ('call', 'reference', 'all') or
                any(type(v) is not str or len(v) > 4096 for v in (prefix, scope))):
            raise ValueError('Invalid bounded query')
        signature = hashlib.sha256(encoded({'generation': self.generation, 'analyzer': self.analyzer_identity,
            'operation': operation, 'seed': seed, 'depth': depth, 'prefix': prefix, 'scope': scope,
            'role': role, 'order': QUERY_RULE_VERSION})).hexdigest()
        started = self._clock()
        if cursor is not None:
            if type(cursor) is not str or len(cursor) != 64 or cursor not in self._continuations:
                raise ValueError('Unknown, evicted or foreign snapshot cursor')
            expiry, expected, payload = self._continuations[cursor]
            if expected != signature or expiry <= started:
                raise ValueError('Changed query or expired snapshot cursor')
            state = json.loads(payload)
        else:
            state = {'frontier': [[seed, 0, 0]], 'visited': [seed], 'matched': 0}
        frontier, visited = deque(state['frontier']), set(state['visited'])
        rows, entities, examined, reason = [], set(), 0, None
        adjacency = self._incoming if operation in ('callers', 'impact') else self._outgoing
        recursive = operation in ('reachable', 'impact')

        def stopped():
            if cancel is not None and cancel():
                return 'cancelled'
            return 'deadline_exceeded' if self._clock() - started >= limits.timeout_seconds else None

        def response(continuation=None, stop=None):
            return {'generation': self.generation, 'rows': rows, 'examined_relationships': examined,
                    'returned_entities': len(entities - {seed}), 'returned_symbol_handles': len(entities),
                    'returned_edges': len(rows),
                    'total_count': {'value': state['matched'], 'kind': 'lower_bound' if frontier else 'exact'},
                    'cursor': continuation, 'truncated': bool(frontier), 'stop_reason': stop}

        # Reserve the complete fixed-size cursor/count envelope before any work.
        if len(encoded(response('0' * 64, 'response_byte_budget_exceeded'))) > limits.max_response_bytes:
            raise ValueError('Query byte limit cannot fit the minimum envelope')
        while frontier:
            reason = stopped()
            if reason:
                break
            node, level, offset = frontier[0]
            relations = adjacency.get(node, ())
            if offset >= len(relations) or recursive and level >= depth:
                frontier.popleft(); continue
            if not state.get('pending') and examined >= limits.max_examined_relationships:
                reason = 'work_budget_exceeded'; break
            if len(rows) >= limits.max_edges:
                reason = 'edge_budget_exceeded'; break
            pending = state.pop('pending', None)
            if pending is None:
                key = relations[offset]
                examined += 1  # Before scope/name/role rejection, never a post-traversal trim.
                site = self._sites[key[0]]
                target = site['caller'] if operation in ('callers', 'impact') else key[1]
                if (role != 'all' and site['role'] != role or not site['path'].startswith(scope) or
                        prefix and (target is None or not self._definitions[target]['name'].startswith(prefix))):
                    frontier[0][2] += 1; continue
                pending = {'row': self._row(key), 'target': target}
            else:
                target = pending['target']
            candidate_entities = entities | _row_entities(pending['row'])
            if len(candidate_entities) > limits.max_entities:
                state['pending'] = pending
                reason = 'entity_budget_exceeded'; break
            row = pending['row']
            previous_entities = entities
            entities = candidate_entities
            state['matched'] += 1
            rows.append(row)
            if len(encoded(response('0' * 64, 'response_byte_budget_exceeded'))) > limits.max_response_bytes:
                rows.pop(); entities = previous_entities; state['matched'] -= 1
                # Retain already examined compact output; resuming does not reread the relation.
                state['pending'] = pending
                reason = 'response_byte_budget_exceeded'; break
            frontier[0][2] += 1
            if recursive and target is not None and target not in visited and level + 1 < depth:
                visited.add(target); frontier.append([target, level + 1, 0])
        if frontier and not reason:
            reason = 'work_budget_exceeded'
        state.update(frontier=list(frontier), visited=sorted(visited))
        continuation = self._cursor(state, signature) if frontier and reason not in ('cancelled', 'deadline_exceeded') else None
        result = response(continuation, reason)
        if len(encoded(result)) > limits.max_response_bytes:
            raise ValueError('Query response exceeded reserved envelope')
        return result


def _selfcheck():
    """Small dependency-free pagination check for the finite comparison adapter."""
    digest = 'a' * 64
    def fact(identifier, start):
        return {'id': identifier, 'path': 'fixture.py',
            'range': {'start_byte': start, 'end_byte': start + 1, 'start_line': 1, 'end_line': 1},
            'provenance': {'source_sha256': digest}}
    definitions = [dict(fact('caller', 0), name='caller'), dict(fact('target', 1), name='target')]
    sites = [dict(fact('site' + str(i), i + 2), caller='caller', role='call',
        targets=['target'] if i != 1 else [], certainty='resolved' if i != 1 else 'unresolved',
        targets_exhaustive=i != 1, reason='' if i != 1 else 'unknown parameter') for i in (2, 1, 0)]
    snapshot = Snapshot({'definitions': definitions, 'sites': sites}, digest, digest)
    cursor, rows, work = None, [], 0
    for _ in range(4):
        page = snapshot.query('caller', limits=Limits(max_edges=1), cursor=cursor)
        rows.extend(page['rows'])
        work += page['examined_relationships']
        cursor = page['cursor']
        if cursor is None:
            break
    assert [row['site']['id'] for row in rows] == ['site0', 'site1', 'site2']
    assert rows[1]['target'] is None and work == 3
    assert page['total_count'] == {'value': 3, 'kind': 'exact'}
    held = Snapshot({'definitions': definitions, 'sites': sites}, digest, digest)
    blocked = held.query('caller', limits=Limits(max_entities=1))
    assert blocked['rows'] == [] and blocked['stop_reason'] == 'entity_budget_exceeded'
    resumed = held.query('caller', cursor=blocked['cursor'], limits=Limits(max_entities=2, max_edges=1))
    assert resumed['examined_relationships'] == 0 and resumed['returned_entities'] == 1
    assert resumed['returned_symbol_handles'] == 2
    print('finite occurrence pagination self-check passed')


class _Stopped(Exception):
    pass


class SQLSnapshot(Snapshot):
    """Own one immutable, read-only shared-index connection, never a fact mirror.

    Opening uses search.connect's guarded disk snapshot with cooperative chunk
    and lock checks. Copy and SQLite setup share the constructor's deadline.
    The caller closes this finite snapshot after its continuation session.
    """
    _order_columns = ('path', 'site_start', 'site_end', 'role', 'target_path',
                      'target_start', 'target_end', 'site_id', 'target_id')
    _symbol_columns = ('path', 'start_byte', 'end_byte', 'id')

    def __init__(self, output, repository_identity=None, output_owner=None, *, clock=time.monotonic,
                 limits=None, cancel=None, cache=None):
        limits = limits or Limits()
        if type(limits) is not Limits or cancel is not None and not callable(cancel):
            raise ValueError('Typed snapshot limits and callable cancellation required')
        self._clock, self._secret, self._continuations = clock, secrets.token_bytes(32), {}
        self._closed, self._active = False, False
        self._work_charge = None
        setup_started, deadline_started, setup_stop = time.monotonic(), self._clock(), None

        def setup_check():
            nonlocal setup_stop
            if cancel is not None and cancel():
                setup_stop = 'cancelled'
            elif self._clock() - deadline_started >= limits.timeout_seconds:
                setup_stop = 'deadline_exceeded'
            if setup_stop:
                raise InterruptedError(setup_stop + ' during structural snapshot setup')

        def setup_progress():
            try:
                setup_check()
            except InterruptedError:
                return 1
            return 0

        self._check = setup_check
        setup_check()
        self.db = connect(Path(output), readonly=True, owner=output_owner, check=setup_check, cache=cache)
        self.snapshot_copy_seconds = time.monotonic() - setup_started
        self.db.set_progress_handler(setup_progress, 64)
        try:
            names = ('schema', 'repository', 'source', 'analyzer', 'config', 'generation')
            metadata = {}
            for name in names:
                item = self._read('SELECT value FROM meta WHERE key=?', ('structural_' + name,))
                if item is not None:
                    metadata['structural_' + name] = item[0]
            for name in names[1:]:
                value = metadata.get('structural_' + name)
                if (type(value) is not str or len(value) != 64 or
                        any(c not in '0123456789abcdef' for c in value)):
                    raise ValueError('A ready structural snapshot with captured identities is required')
            if repository_identity is None:
                repository_identity = metadata['structural_repository']
            if metadata['structural_repository'] != repository_identity:
                raise ValueError('Structural snapshot belongs to another repository')
            ordinary = self._read("SELECT value FROM meta WHERE key='repository'")
            if ordinary and ordinary[0] != repository_identity:
                raise ValueError('Shared snapshot belongs to another repository')
            self.repository_identity = repository_identity
            self.config_identity = metadata['structural_config']
            self._source_identity, self._analyzer_identity = metadata['structural_source'], metadata['structural_analyzer']
            self._generation, self.schema = metadata['structural_generation'], metadata['structural_schema']
            impact = self._read("SELECT substr(value,1,32769) FROM meta WHERE key='structural_impact_receipt'")
            self.impact_receipt = None
            if impact is not None:
                if len(impact[0].encode()) > 32768:
                    raise ValueError('Impact receipt exceeds its captured metadata ceiling')
                self.impact_receipt = json.loads(impact[0])
            if type(self.schema) is not str or not self.schema.startswith('structural-'):
                raise ValueError('Invalid structural snapshot schema')
            # Missing projections fail closed; queries cannot silently fall back to
            # an unindexed scan or a per-query sort of the captured graph.
            expected = {'structural_symbol_order': self._symbol_columns,
                'structural_outgoing': ('caller_id',) + self._order_columns,
                'structural_incoming': ('target_id',) + self._order_columns[:-1],
                'structural_occurrence_order': self._order_columns}
            for index, columns in expected.items():
                setup_check()
                if self._read("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)) is None:
                    raise ValueError('Structural snapshot lacks bounded query projections; refresh the index')
                actual = tuple(row[2] for row in self.db.execute('PRAGMA index_info(' + index + ')'))
                setup_check()
                if actual != columns:
                    raise ValueError('Structural query projection has incompatible ordering')
        except BaseException:
            self.close()
            raise
        finally:
            if not self._closed:
                self.db.set_progress_handler(None, 0)
            self._check = None
        self.storage_setup_seconds = time.monotonic() - setup_started

    def close(self):
        if not self._closed:
            self._closed = True
            self._continuations.clear()
            self.db.close()

    def __enter__(self):
        if self._closed:
            raise ValueError('Closed structural snapshot')
        return self

    def __exit__(self, *_):
        self.close()

    def _read(self, sql, parameters=()):
        self._check()
        if self._work_charge is not None:
            self._work_charge()
        try:
            row = self.db.execute(sql, parameters).fetchone()
        except sqlite3.OperationalError:
            self._check()
            raise
        self._check()
        return row

    def _handle(self, identifier):
        if identifier is None:
            return None
        item = self._read('''SELECT s.id,s.path,
            json_extract(s.data,'$.range') AS span,
            json_type(s.data,'$.range') AS span_type,
            substr(json_extract(s.data,'$.name'),1,256) AS name,
            json_type(s.data,'$.name') AS name_type,
            length(json_extract(s.data,'$.name')) > 256 AS name_truncated,
            json_extract(s.data,'$.provenance.source_sha256') AS digest,
            json_extract(f.record,'$.sha256') AS file_digest
            FROM structural_symbols s JOIN structural_files f ON f.path=s.path WHERE s.id=?''', (identifier,))
        if item is None:
            raise ValueError('Unknown structural symbol')
        return self._source_handle(item, name=True)

    @staticmethod
    def _source_handle(item, *, name=False):
        span = json.loads(item['span'])
        digest = item['digest']
        if (type(item['id']) is not str or len(item['id']) > 8192 or
                type(item['path']) is not str or len(item['path']) > 4096 or
                item['span_type'] != 'object' or
                type(span) is not dict or set(span) != {'start_byte', 'end_byte', 'start_line', 'end_line'} or
                any(type(v) is not int for v in span.values()) or
                not 0 <= span['start_byte'] <= span['end_byte'] or
                not 1 <= span['start_line'] <= span['end_line'] or
                type(digest) is not str or len(digest) != 64 or
                any(c not in '0123456789abcdef' for c in digest) or digest != item['file_digest']):
            raise ValueError('Invalid persisted source handle')
        handle = {'id': item['id'], 'path': item['path'], 'range': span, 'source_sha256': digest}
        if name:
            if item['name_type'] != 'text' or type(item['name']) is not str:
                raise ValueError('Invalid persisted declaration name')
            handle.update(name=item['name'], name_truncated=bool(item['name_truncated']))
        return handle

    def _row(self, key):
        site = self._read('''SELECT s.id,s.path,s.role,
            json_extract(s.data,'$.range') AS span,
            json_type(s.data,'$.range') AS span_type,
            json_extract(s.data,'$.caller') AS caller,
            json_type(s.data,'$.caller') AS caller_type,
            json_extract(s.data,'$.certainty') AS certainty,
            json_extract(s.data,'$.targets_exhaustive') AS exhaustive,
            json_type(s.data,'$.targets_exhaustive') AS exhaustive_type,
            coalesce(substr(json_extract(s.data,'$.reason'),1,256),'') AS reason,
            coalesce(length(json_extract(s.data,'$.reason')) > 256,0) AS reason_truncated,
            json_extract(s.data,'$.provenance.source_sha256') AS digest,
            json_extract(f.record,'$.sha256') AS file_digest
            FROM structural_sites s JOIN structural_files f ON f.path=s.path WHERE s.id=?''', (key[0],))
        if (site is None or site['role'] not in ('call', 'reference', 'framework', 'framework_boundary') or
                site['certainty'] not in ('resolved', 'candidate', 'unresolved') or
                site['exhaustive_type'] not in ('true', 'false') or
                site['caller_type'] not in ('text', 'null') or type(site['reason']) is not str):
            raise ValueError('Invalid persisted occurrence')
        handle = self._source_handle(site)
        handle['role'] = site['role']
        result = {'site': handle, 'caller': self._handle(site['caller']), 'target': self._handle(key[1]),
                'certainty': site['certainty'], 'targets_exhaustive': bool(site['exhaustive']),
                'reason': site['reason'], 'reason_truncated': bool(site['reason_truncated'])}
        if site['role'] in ('framework', 'framework_boundary'):
            extra = self._read('''SELECT substr(json_extract(data,'$.relation_kind'),1,128),
                substr(json_extract(data,'$.candidate_relation_kind'),1,128),
                json_extract(data,'$.framework_identity_asserted'),json_extract(data,'$.partial'),
                substr(json_extract(data,'$.partial_source_role'),1,128),
                substr(json_extract(data,'$.evidence'),1,65537) FROM structural_sites WHERE id=?''', (key[0],))
            if len(extra[5].encode()) > 65536: raise ValueError('Framework witness byte ceiling exhausted')
            result.update(family=site['role'], relation_kind=extra[0], candidate_relation_kind=extra[1],
                          framework_identity_asserted=bool(extra[2]), partial=bool(extra[3]),
                          partial_source_role=extra[4], evidence=json.loads(extra[5]), runtime_qualified=False)
        return result

    def _next(self, node, after, operation):
        symbols = operation == 'symbol'
        columns = self._symbol_columns if symbols else self._order_columns
        index = 'structural_symbol_order' if symbols else ('structural_incoming' if operation in
            ('callers', 'impact') else 'structural_outgoing' if node is not None else 'structural_occurrence_order')
        table = 'structural_symbols' if symbols else 'structural_relationships'
        clauses, arguments = [], []
        if node is not None:
            clauses.append(('id' if symbols else 'target_id' if operation in ('callers', 'impact') else 'caller_id') + '=?')
            arguments.append(node)
        if after is not None:
            clauses.append('(' + ','.join(columns) + ') > (' + ','.join('?' for _ in columns) + ')')
            arguments.extend(after)
        select = ','.join(columns) + ('' if symbols else ',caller_id')
        indexed = '' if symbols and node is not None else ' INDEXED BY ' + index
        return self._read('SELECT ' + select + ' FROM ' + table + indexed +
            (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY ' + ','.join(columns) +
            ' LIMIT 1', arguments)

    def _require_impact(self):
        receipt = self.impact_receipt
        expected = dict(repository_identity=self.repository_identity, source_identity=self.source_identity,
            analyzer_identity=self.analyzer_identity, config_identity=self.config_identity, generation=self.generation)
        validate_impact_receipt(receipt, expected)
        schema = self._read("SELECT value FROM meta WHERE key='structural_impact_schema'")
        if schema is None or schema[0] != IMPACT_RULE_VERSION:
            raise ValueError('Missing impact projection version; refresh the index')
        for name, columns in (('structural_reverse_import', 'target_path,path,start_byte,end_byte,id'),
                              ('structural_unassigned_occurrence', 'path,target_id,site_start,site_end,role,target_path,target_start,target_end,site_id')):
            row = self._read("SELECT group_concat(name,',') FROM (SELECT name FROM pragma_index_info(?) ORDER BY seqno)", (name,))
            if row is None or row[0] != columns:
                raise ValueError('Missing bounded import projection; refresh the index')
        return receipt

    def _file_handle(self, path):
        row = self._read('SELECT record,status,kind FROM structural_files WHERE path=?', (path,))
        if row is None:
            return None
        record = json.loads(row['record'])
        digest = record.get('sha256')
        if (record.get('path') != path or type(record.get('bytes')) is not int or record['bytes'] < 0 or
                type(digest) is not str or len(digest) != 64 or
                any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid captured impact file handle')
        return {'id': 'file:' + hashlib.sha256(path.encode()).hexdigest(), 'path': path,
                'source_sha256': digest, 'source_bytes': record['bytes'], 'admission_status': row['status'], 'kind': row['kind']}

    def _import_row(self, occurrence):
        item = json.loads(occurrence['data'])
        owner = self._file_handle(occurrence['path'])
        if (owner is None or item.get('source_sha256') != owner['source_sha256'] or item.get('id') != occurrence['id'] or
                item.get('path') != occurrence['path'] or item.get('certainty') != occurrence['certainty'] or
                item.get('certainty') not in ('resolved', 'candidate', 'unresolved') or
                item.get('provenance', {}).get('source_sha256') != owner['source_sha256'] or
                item.get('provenance', {}).get('evidence_kind') != 'static_syntax' or
                item.get('range', {}).get('start_byte') != occurrence['start_byte'] or
                item.get('range', {}).get('end_byte') != occurrence['end_byte'] or
                item['range']['end_byte'] > owner['source_bytes'] or
                item['id'] != 'import:' + hashlib.sha256(encoded([item['path'], occurrence['ordinal'], item['range']])).hexdigest()):
            raise ValueError('Foreign captured import provenance')
        handle = self._source_handle({'id': item['id'], 'path': item['path'], 'span': encoded(item['range']),
            'span_type': 'object', 'digest': item['source_sha256'], 'file_digest': owner['source_sha256']})
        target = self._file_handle(occurrence['target_path']) if occurrence['target_path'] else None
        if occurrence['target_path'] and target is None:
            raise ValueError('Missing captured import target')
        return {'relation': 'import', 'site': dict(handle, role='import'), 'importer': owner, 'target': target,
            'certainty': occurrence['certainty'], 'targets_exhaustive': False,
            'source_candidates_exhaustive': bool(item.get('source_candidates_exhaustive')),
            'reason': item['reason'][:256], 'reason_truncated': len(item['reason']) > 256,
            'evidence_kind': 'static_syntax', 'scope': item['scope']}

    def _impact_query(self, seed, *, selector, relations, certainties, depth, prefix, scope, role,
                      limits, cursor, cancel, setup):
        """One SQL/work/deadline budget for selection, imports and reverse calls."""
        selector, relations, certainties = _impact_options(selector, relations, certainties)
        started, reason, work, callbacks = self._clock(), None, 0, 0
        rows, selected_files, selected_symbols, unavailable_paths = [], [], [], []
        entities, symbols, file_ids = set(), set(), set()
        receipt, signature, state = None, None, None
        def check():
            nonlocal reason
            if cancel is not None and cancel(): reason = 'cancelled'
            elif self._clock() - started >= limits.timeout_seconds: reason = 'deadline_exceeded'
            if reason in ('cancelled', 'deadline_exceeded'): raise _Stopped()
        def charge():
            nonlocal work, reason
            if work >= limits.max_examined_relationships:
                reason = 'work_budget_exceeded'
                raise _Stopped()
            work += 1
        def progress():
            nonlocal callbacks
            callbacks += 1
            try: check()
            except _Stopped: return 1
            return 0
        self._check, self._active, self._work_charge = check, True, charge
        self.db.set_progress_handler(progress, 64)
        frontier, visited, seen, selected = deque(), {}, set(), set()
        def boundary(kind):
            state['boundaries'][kind] = state['boundaries'].get(kind, 0) + 1
        def queue(kind, identifier, level):
            key = kind + ':' + identifier
            if level >= depth:
                boundary('depth_limit')
            elif key not in visited or visited[key] > level:
                visited[key] = level
                frontier.append([kind, identifier, level, None])
        def file_work(path, level, membership=True):
            if membership and 'call' in relations: queue('members', path, level)
            if 'import' in relations:
                queue('imports', path, level)
                queue('unknown_imports', path, level)
            if 'call' in relations: queue('unknown_calls', path, level)
        def response(continuation=None, reserved=False):
            boundaries = dict((state or {}).get('boundaries', {}))
            if reserved:
                boundaries.update({key: 2 ** 64 for key in ('depth_limit', 'unresolved_call', 'unresolved_import',
                    'candidate_call', 'candidate_import', 'filtered_relation', 'filtered_name',
                    'source_area_not_in_admitted_inventory', 'historical_or_nonadmitted_source_path',
                    'partial_excluded_or_configuration_source')})
            return {'generation': self.generation, 'repository_identity': self.repository_identity,
                'source_identity': self.source_identity, 'analyzer_identity': self.analyzer_identity,
                'config_identity': self.config_identity, 'impact_identity': (receipt or {}).get('identity'),
                'impact_schema': IMPACT_RULE_VERSION, 'rows': rows,
                'selected_files': selected_files, 'selected_symbols': selected_symbols,
                'unavailable_paths': unavailable_paths,
                'selection': {'selector': selector, 'seed': seed, 'basis': 'current_captured_definitions',
                    'git_change': (receipt or {}).get('git_change'), 'base_snapshot': (receipt or {}).get('base_snapshot'),
                    'revision_dirty': (receipt or {}).get('revision_dirty')},
                'scope': {'path_filter': scope, 'name_prefix': prefix, 'depth': depth,
                    'relations': relations, 'certainties': certainties, 'role': role,
                    'evidence_kind': 'static_syntax', 'claim': 'possible_captured_reachability'},
                'unknown_boundaries': boundaries,
                'historical_call_closure': 'unavailable_current_index_only', 'contracts_available': False,
                'runtime_complete': False, 'live_source_observed': False,
                'examined_work': work, 'examined_relationships': work, 'examined_symbols': len(selected_symbols),
                'returned_entities': len(entities), 'returned_symbol_handles': len(symbols),
                'returned_file_handles': len(file_ids), 'returned_edges': len(rows), 'excerpt_bytes': 0,
                'storage_progress_callbacks': 2 ** 64 if reserved else callbacks,
                'storage_setup_seconds': self.storage_setup_seconds if setup is None else setup[0],
                'snapshot_copy_seconds': self.snapshot_copy_seconds if setup is None else setup[1],
                'total_count': {'value': (state or {}).get('matched', 0),
                    'kind': 'lower_bound' if frontier or reason or (state or {}).get('boundaries') else 'exact',
                    'scope': 'captured_physical_relations_within_selected_filters_and_depth'},
                'cursor': continuation, 'truncated': bool(frontier) or reason is not None, 'stop_reason': reason}
        try:
            receipt = self._require_impact()
            if selector and selector['kind'] == 'git_change' and (receipt['git_change'].get('status') != 'ready' or
                    receipt['git_change'].get('base_revision') != selector['base_revision']):
                raise ValueError('Git selector has no matching admitted changed-path receipt')
            signature = hashlib.sha256(encoded({'impact': receipt, 'implementation': code_identity(),
                'rules': IMPACT_RULE_VERSION, 'seed': seed, 'selector': selector, 'relations': relations,
                'certainties': certainties, 'depth': depth, 'prefix': prefix, 'scope': scope, 'role': role})).hexdigest()
            if cursor is not None:
                previous = self._continuations.get(cursor)
                if previous is None or previous[0] <= started or previous[1] != signature:
                    raise ValueError('Changed impact selection or expired snapshot cursor')
                state = json.loads(previous[2])
                frontier, visited, seen = deque(state['frontier']), state['visited'], set(state['seen'])
                selected = set(state['selected'])
            else:
                starts = ([['seed', seed, 0, None]] if seed else
                    [['git', '', 0, None]] if selector['kind'] == 'git_change' else
                    [['area', path, 0, None] for path in selector['paths']])
                frontier = deque(starts)
                state = {'frontier': starts, 'visited': {}, 'seen': [], 'selected': [], 'matched': 0,
                    'boundaries': {'unassigned_incoming_targets_not_enumerable': 1}}
                if selector and selector['kind'] == 'git_change':
                    boundary('working_tree_commit_byte_affinity_unobserved')
                    boundary('historical_preimage_call_closure_unavailable')
            if len(encoded(response('0' * 64, True))) > limits.max_response_bytes:
                raise ValueError('Impact byte limit cannot fit the minimum envelope')
            while frontier:
                check()
                kind, identifier, level, after = frontier[0]
                pending = state.get('pending')
                if pending is None:
                    occurrence, position, value, output, sym, files, actions, rotate = None, None, None, None, [], [], [], False
                    if kind == 'seed':
                        value = self._handle(identifier)
                        output, sym, position = 'symbol', [identifier], []
                        actions = ([('calls', identifier, level)] if 'call' in relations else []) + [('file', value['path'], level)]
                    elif kind in ('area', 'git'):
                        if kind == 'git':
                            occurrence = self._read('SELECT path,status FROM structural_git_changes' +
                                (' WHERE path>?' if after else '') + ' ORDER BY path LIMIT 1', (after,) if after else ())
                        else:
                            clauses, args = [], []
                            if identifier != '.':
                                if identifier.endswith('/'):
                                    clauses += ['path>=?', 'path<?']; args += [identifier, identifier[:-1] + '0']
                                else: clauses += ['path=?']; args += [identifier]
                            if after: clauses += ['path>?']; args += [after]
                            occurrence = self._read('SELECT path,status FROM structural_files' +
                                (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY path LIMIT 1', args)
                        if occurrence is None:
                            if kind == 'area' and after is None: boundary('source_area_not_in_admitted_inventory')
                            frontier.popleft(); continue
                        position, rotate = occurrence['path'], True
                        value = self._file_handle(position)
                        if value is None:
                            value = {'id': 'unavailable-source:' + hashlib.sha256(position.encode()).hexdigest(),
                                     'path': position, 'change_status': occurrence['status'], 'source_sha256': None,
                                     'reason': 'historical or nonadmitted source; current closure unavailable'}
                            output = 'boundary'
                        else:
                            if kind == 'git': value['change_status'] = occurrence['status']
                            output, files = 'file', [value['id']]
                            actions = [('file_members', position, level)]
                    elif kind in ('file', 'file_members'):
                        file_work(identifier, level, kind == 'file_members')
                        frontier.popleft(); continue
                    elif kind == 'members':
                        occurrence = self._read('SELECT id,start_byte,end_byte FROM structural_symbols INDEXED BY structural_symbol_order WHERE path=?' +
                            (' AND (start_byte,end_byte,id)>(?,?,?)' if after else '') +
                            ' ORDER BY start_byte,end_byte,id LIMIT 1', [identifier] + (after or []))
                        if occurrence is None: frontier.popleft(); continue
                        position = [occurrence['start_byte'], occurrence['end_byte'], occurrence['id']]
                        value = self._handle(occurrence['id'])
                        output, sym = 'symbol', [value['id']]
                        actions = [('calls', value['id'], level)]
                    elif kind in ('imports', 'unknown_imports'):
                        columns = ('path', 'start_byte', 'end_byte', 'id') if kind == 'imports' else ('start_byte', 'end_byte', 'id', 'target_path')
                        clause, args = ('target_path=?', [identifier]) if kind == 'imports' else ("path=? AND target_path=''", [identifier])
                        if after:
                            clause += ' AND (' + ','.join(columns) + ')>(' + ','.join('?' for _ in columns) + ')'; args += after
                        occurrence = self._read('SELECT * FROM structural_import_relationships INDEXED BY structural_reverse_import' +
                            ' WHERE ' + clause + ' ORDER BY ' + ','.join(columns) + ' LIMIT 1', args)
                        if occurrence is None: frontier.popleft(); continue
                        position = [occurrence[k] for k in columns]
                        value = self._import_row(occurrence)
                        output = 'edge'
                        files = [h['id'] for h in (value['importer'], value['target']) if h]
                        if kind == 'imports': actions = [('file_members', value['importer']['path'], level + 1)]
                    else:
                        if kind == 'calls': occurrence = self._next(identifier, after, 'impact')
                        else:
                            columns = self._order_columns
                            args = [identifier] + (after or [])
                            occurrence = self._read("SELECT * FROM structural_relationships INDEXED BY structural_unassigned_occurrence WHERE path=? AND target_id=''" +
                                (' AND (' + ','.join(columns) + ')>(' + ','.join('?' for _ in columns) + ')' if after else '') +
                                ' ORDER BY ' + ','.join(columns) + ' LIMIT 1', args)
                        if occurrence is None: frontier.popleft(); continue
                        position = [occurrence[k] for k in self._order_columns]
                        value = dict(self._row((occurrence['site_id'], occurrence['target_id'] or None)),
                                     relation='call', evidence_kind='static_syntax')
                        output = 'edge'
                        sym = [h['id'] for h in (value['caller'], value['target']) if h]
                        if kind == 'calls' and value['caller']:
                            actions = [('calls', value['caller']['id'], level + 1), ('file', value['caller']['path'], level + 1)]
                    if output == 'edge':
                        edge_key = value['relation'] + ':' + value['site']['id'] + ':' + (value['target']['id'] if value['target'] else '')
                        if value['target'] is None: boundary('unresolved_' + value['relation'])
                        if value['certainty'] == 'candidate': boundary('candidate_' + value['relation'])
                        path = value['site']['path']
                        allowed_scope = not scope or path == scope.rstrip('/') or path.startswith(scope.rstrip('/') + '/')
                        if (value['relation'] not in relations or value['certainty'] not in certainties or not allowed_scope or
                                value['relation'] == 'call' and role != 'all' and value['site']['role'] != role):
                            if edge_key not in seen: boundary('filtered_relation')
                            frontier[0][3] = position; continue
                        if prefix:
                            target = value.get('caller')
                            name = None if target is None else self._read(
                                "SELECT substr(json_extract(data,'$.name'),1,?) FROM structural_symbols WHERE id=?", (len(prefix), target['id']))
                            if name is None or name[0] != prefix:
                                boundary('filtered_name'); frontier[0][3] = position; continue
                        if edge_key in seen:
                            output, sym, files, edge_key = 'internal', [], [], None
                    else: edge_key = None
                    selection_key = output + ':' + value['id'] if output in ('file', 'symbol', 'boundary') else None
                    if selection_key in selected:
                        output, sym, files, selection_key = 'internal', [], [], None
                    pending = {'output': output, 'value': value, 'symbols': sym, 'files': files,
                               'position': position, 'actions': actions, 'rotate': rotate, 'edge_key': edge_key,
                               'selection_key': selection_key}
                    state['pending'] = pending
                candidate = entities | set(pending['symbols']) | set(pending['files']) | (
                    {pending['value']['id']} if pending['output'] == 'boundary' else set())
                if len(candidate) > limits.max_entities:
                    reason = 'entity_budget_exceeded'; break
                if pending['output'] == 'edge' and len(rows) >= limits.max_edges:
                    reason = 'edge_budget_exceeded'; break
                bucket = {'file': selected_files, 'symbol': selected_symbols, 'edge': rows, 'boundary': unavailable_paths}.get(pending['output'])
                old_entities, old_symbols, old_files = entities, symbols, file_ids
                entities, symbols, file_ids = candidate, symbols | set(pending['symbols']), file_ids | set(pending['files'])
                if bucket is not None: bucket.append(pending['value'])
                if pending['output'] == 'edge': state['matched'] += 1
                if len(encoded(response('0' * 64, True))) > limits.max_response_bytes:
                    if bucket is not None: bucket.pop()
                    entities, symbols, file_ids = old_entities, old_symbols, old_files
                    if pending['output'] == 'edge': state['matched'] -= 1
                    reason = 'response_byte_budget_exceeded'; break
                if pending['output'] == 'boundary': boundary('historical_or_nonadmitted_source_path')
                elif pending['output'] == 'file' and pending['value']['admission_status'] not in ('parsed',):
                    boundary('partial_excluded_or_configuration_source')
                if pending['edge_key']: seen.add(pending['edge_key'])
                if pending['selection_key']: selected.add(pending['selection_key'])
                if kind == 'seed': frontier.popleft()
                else: frontier[0][3] = pending['position']
                for action in pending['actions']: queue(*action)
                if pending['rotate'] and frontier: frontier.rotate(-1)
                state.pop('pending', None)
                state.update(frontier=list(frontier), visited=visited, seen=sorted(seen), selected=sorted(selected))
                if len(encoded(state)) > 65536:
                    reason = 'continuation_state_budget_exceeded'; break
        except _Stopped:
            pass
        finally:
            self.db.set_progress_handler(None, 0)
            self._active, self._check, self._work_charge = False, None, None
        if state is None:
            return response()
        state.update(frontier=list(frontier), visited=visited, seen=sorted(seen), selected=sorted(selected))
        if len(encoded(state)) > 65536: reason = 'continuation_state_budget_exceeded'
        previous = self._continuations.pop(cursor, None) if cursor is not None else None
        try:
            continuation = self._cursor(state, signature) if frontier and signature is not None and reason not in (
                'cancelled', 'deadline_exceeded', 'continuation_state_budget_exceeded') else None
        except BaseException:
            if previous is not None: self._continuations[cursor] = previous
            raise
        result = response(continuation)
        if len(encoded(result)) > limits.max_response_bytes:
            raise ValueError('Impact response exceeded its reserved envelope')
        return result

    def query(self, seed=None, *, operation='callees', depth=2, prefix='', scope='', role='call',
              limits=None, cursor=None, cancel=None, _setup=None, selector=None, relations=None, certainties=None,
              families=None, kinds=None):
        """Return source handles with BFS reachability and indexed occurrence pages.

        call/reference list all occurrences, or a seed's outgoing occurrences.
        symbol lists declarations, or one exact ID. Prefix filters use the full
        declaration name; rejected rows still consume the examined-work budget.
        Excerpts are omitted, so their byte usage is zero under every limit.
        """
        limits = limits or Limits()
        if self._closed or self._active or type(limits) is not Limits:
            raise ValueError('Invalid bounded persisted query')
        _validate_query(seed, operation, depth, prefix, scope, role, cancel, selector, relations, certainties)
        families, kinds = _framework_filters(operation, families, kinds)
        if selector is not None or relations is not None or certainties is not None:
            return self._impact_query(seed, selector=selector, relations=relations, certainties=certainties,
                depth=depth, prefix=prefix, scope=scope, role=role, limits=limits, cursor=cursor, cancel=cancel, setup=_setup)
        requested_role = role
        if operation in ('call', 'reference'):
            role = 'call' if operation == 'call' else 'reference'
        elif operation == 'framework':
            role = 'all'
        started, reason, storage_callbacks = self._clock(), None, 0
        signature = hashlib.sha256(encoded({'generation': self.generation,
            'repository': self.repository_identity, 'source': self.source_identity,
            'analyzer': self.analyzer_identity, 'config': self.config_identity,
            'schema': self.schema, 'implementation': code_identity(), 'order': QUERY_RULE_VERSION,
            'impact': self.impact_receipt if operation == 'impact' else None,
            'operation': operation, 'seed': seed, 'depth': depth, 'prefix': prefix, 'scope': scope,
            'role': role, 'requested_role': requested_role, 'families': families, 'kinds': kinds})).hexdigest()
        if cursor is not None:
            if type(cursor) is not str or len(cursor) != 64 or cursor not in self._continuations:
                raise ValueError('Unknown, evicted or foreign snapshot cursor')
            expiry, expected, payload = self._continuations[cursor]
            if expected != signature or expiry <= started:
                raise ValueError('Changed query or expired snapshot cursor')
            state = json.loads(payload)
        else:
            state = {'frontier': [[seed, 0, None]], 'visited': [seed] if seed else [], 'matched': 0}
        frontier, visited = deque(state['frontier']), set(state['visited'])
        rows, entities, examined = [], set(), 0
        symbols, reverse = operation == 'symbol', operation in ('callers', 'impact')
        recursive = operation in ('reachable', 'impact')

        def stopped():
            nonlocal reason
            if cancel is not None and cancel():
                reason = 'cancelled'
            elif self._clock() - started >= limits.timeout_seconds:
                reason = 'deadline_exceeded'
            return reason in ('cancelled', 'deadline_exceeded')

        def check():
            if stopped():
                raise _Stopped()

        def progress():
            nonlocal storage_callbacks
            storage_callbacks += 1
            return 1 if stopped() else 0

        def response(continuation=None, stop=None, *, reserved=False):
            return {'generation': self.generation, 'repository_identity': self.repository_identity,
                'source_identity': self.source_identity, 'analyzer_identity': self.analyzer_identity,
                'config_identity': self.config_identity,
                'rows': rows, 'examined_relationships': 0 if symbols else examined,
                'examined_symbols': examined if symbols else 0,
                'returned_entities': len(entities - {seed}), 'returned_symbol_handles': len(entities),
                'returned_edges': 0 if symbols else len(rows), 'excerpt_bytes': 0,
                'storage_progress_callbacks': 2 ** 64 if reserved else storage_callbacks,
                'storage_setup_seconds': self.storage_setup_seconds if _setup is None else _setup[0],
                'snapshot_copy_seconds': self.snapshot_copy_seconds if _setup is None else _setup[1],
                'total_count': {'value': state['matched'], 'kind': 'lower_bound' if frontier else 'exact'},
                'cursor': continuation, 'truncated': bool(frontier), 'stop_reason': stop}

        if len(encoded(response('0' * 64, 'continuation_state_budget_exceeded', reserved=True))) > limits.max_response_bytes:
            raise ValueError('Query byte limit cannot fit the minimum envelope')
        self._check, self._active = check, True
        self.db.set_progress_handler(progress, 64)
        try:
            check()
            if seed is not None:
                if self._read('SELECT 1 FROM structural_symbols WHERE id=?', (seed,)) is None:
                    raise ValueError('Unknown structural seed')
            while frontier:
                check()
                node, level, after = frontier[0]
                if recursive and level >= depth:
                    frontier.popleft()
                    continue
                pending = state.get('pending')
                if pending is None and examined >= limits.max_examined_relationships:
                    reason = 'work_budget_exceeded'
                    break
                if len(rows) >= limits.max_edges:
                    reason = 'edge_budget_exceeded'
                    break
                occurrence = None if pending else self._next(node, after, operation)
                if pending is None and occurrence is None:
                    frontier.popleft()
                    continue
                if pending is None:
                    examined += 1
                    columns = self._symbol_columns if symbols else self._order_columns
                    position = [occurrence[name] for name in columns]
                    if not occurrence['path'].startswith(scope) or not symbols and role != 'all' and occurrence['role'] != role:
                        frontier[0][2] = position
                        continue
                    if operation == 'framework' and occurrence['role'] not in families:
                        frontier[0][2] = position
                        continue
                    if symbols:
                        target = occurrence['id']
                        row = self._handle(target)
                    else:
                        target = (occurrence['caller_id'] if reverse else occurrence['target_id']) or None
                        row = self._row((occurrence['site_id'], occurrence['target_id'] or None))
                    if operation == 'framework' and row['relation_kind'] not in kinds:
                        frontier[0][2] = position
                        continue
                    if prefix:
                        full_name = None if target is None else self._read(
                            "SELECT substr(json_extract(data,'$.name'),1,?) FROM structural_symbols WHERE id=?",
                            (len(prefix), target))
                        if full_name is None or full_name[0] != prefix:
                            frontier[0][2] = position
                            continue
                    pending = {'row': row, 'target': target, 'position': position}
                candidate_entities = entities | _row_entities(pending['row'])
                if len(candidate_entities) > limits.max_entities:
                    state['pending'] = pending
                    reason = 'entity_budget_exceeded'
                    break
                previous_entities = entities
                entities = candidate_entities
                state['matched'] += 1
                rows.append(pending['row'])
                if len(encoded(response('0' * 64, 'continuation_state_budget_exceeded', reserved=True))) > limits.max_response_bytes:
                    rows.pop()
                    entities = previous_entities
                    state['matched'] -= 1
                    state['pending'] = pending
                    reason = 'response_byte_budget_exceeded'
                    break
                state.pop('pending', None)
                frontier[0][2] = pending['position']
                target = pending['target']
                if recursive and target is not None and target not in visited and level + 1 < depth:
                    visited.add(target)
                    frontier.append([target, level + 1, None])
                check()
                # Cursor capacity bounds traversal memory, including rejected rows.
                state.update(frontier=list(frontier), visited=sorted(visited))
                if len(encoded(state)) > 65536:
                    reason = 'continuation_state_budget_exceeded'
                    break
        except _Stopped:
            pass
        finally:
            self.db.set_progress_handler(None, 0)
            self._active = False
            self._check = None
        state.update(frontier=list(frontier), visited=sorted(visited))
        if len(encoded(state)) > 65536:
            reason = 'continuation_state_budget_exceeded'
        # Retire a successfully consumed predecessor, so one long traversal does
        # not occupy every live-session slot. Failed cursor creation is retryable.
        previous = self._continuations.pop(cursor, None) if cursor is not None else None
        try:
            continuation = self._cursor(state, signature) if frontier and reason not in (
                'cancelled', 'deadline_exceeded', 'continuation_state_budget_exceeded') else None
        except BaseException:
            if previous is not None:
                self._continuations[cursor] = previous
            raise
        result = response(continuation, reason)
        if len(encoded(result)) > limits.max_response_bytes:
            raise ValueError('Query response exceeded reserved envelope')
        return result


class _RequestStopped(InterruptedError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Queries:
    """Serialized public query sessions over at most four captured databases.

    Each snapshot expires 60 seconds after creation. Up to 32 continuations are
    live across those snapshots; tokens belong only to this owner and are
    consumed on successful continuation. New requests capture the current index.
    Lock, guarded copy, SQLite setup and traversal share one request deadline.
    Checks are cooperative; they do not preempt a blocked kernel operation.
    """
    def __init__(self, output, owner=None, *, repository_identity=None, clock=time.monotonic):
        self.output, self.repository_identity = Path(output), repository_identity
        with SourceRoot(self.output) as boundary:
            if owner is not None and boundary.identity != owner:
                raise ValueError('Query output owner changed')
            self.owner = boundary.identity
        self._clock, self._lock, self._sessions, self._closed = clock, Lock(), [], False

    @staticmethod
    def _dispose(session, check=None):
        if check is None:
            SNAPSHOT_LOCK.acquire()
        else:
            while True:
                check()
                if SNAPSHOT_LOCK.acquire(timeout=.01):
                    break
        try:
            session[0].close()
            temporary = session[1].get('temporary')
            if temporary is not None:
                temporary.cleanup()
            session[1].clear()
            if check is not None:
                check()
        finally:
            SNAPSHOT_LOCK.release()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for session in self._sessions:
                self._dispose(session)
            self._sessions.clear()

    def __enter__(self):
        if self._closed:
            raise ValueError('Closed query sessions')
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _aborted(reason, elapsed, snapshot=None):
        return {'generation': snapshot.generation if snapshot else None,
            'repository_identity': snapshot.repository_identity if snapshot else None,
            'source_identity': snapshot.source_identity if snapshot else None,
            'analyzer_identity': snapshot.analyzer_identity if snapshot else None,
            'config_identity': snapshot.config_identity if snapshot else None,
            'rows': [], 'examined_relationships': 0, 'examined_symbols': 0,
            'returned_entities': 0, 'returned_symbol_handles': 0, 'returned_edges': 0, 'excerpt_bytes': 0,
            'storage_progress_callbacks': 0, 'storage_setup_seconds': elapsed,
            'snapshot_copy_seconds': 0.0, 'total_count': {'value': None, 'kind': 'unknown'},
            'cursor': None, 'truncated': True, 'stop_reason': reason}

    def run(self, payload, cancel=None):
        if (self._closed or type(payload) is not dict or len(payload) > 13 or
                set(payload) - {'seed', 'operation', 'depth', 'prefix', 'scope', 'role', 'limits', 'cursor',
                               'selector', 'relations', 'certainties', 'families', 'kinds'}):
            raise ValueError('Invalid public query payload or closed sessions')
        extra = {'selector', 'relations', 'certainties'} & set(payload)
        if extra and (payload.get('operation') != 'impact' or any(payload[key] is None for key in extra)):
            raise ValueError('Impact selector/filter fields require their explicit impact values')
        if extra:
            bounded = dict(payload)
            if type(bounded.get('limits')) is Limits:
                bounded['limits'] = {key: getattr(bounded['limits'], key) for key in Limits.__dataclass_fields__}
            try:
                size = len(encoded(bounded))
            except (TypeError, ValueError) as error:
                raise ValueError('Impact request must contain bounded JSON values') from error
            if size > 8192:
                raise ValueError('Impact request byte ceiling exhausted')
        values = payload.get('limits')
        if values is None:
            limits = Limits()
        elif type(values) is Limits:
            limits = values
        elif type(values) is dict and len(values) <= len(Limits.__dataclass_fields__) and not set(values) - set(Limits.__dataclass_fields__):
            limits = Limits(**values)
        else:
            raise ValueError('Invalid public query limits')
        arguments = {'seed': payload.get('seed'), 'operation': payload.get('operation', 'callees'),
            'depth': payload.get('depth', 2), 'prefix': payload.get('prefix', ''),
            'scope': payload.get('scope', ''), 'role': payload.get('role', 'call'),
            'selector': payload.get('selector'), 'relations': payload.get('relations'), 'certainties': payload.get('certainties')}
        _validate_query(**arguments, cancel=cancel)
        _framework_filters(arguments['operation'], payload.get('families'), payload.get('kinds'))
        arguments.update(families=payload.get('families'), kinds=payload.get('kinds'))
        cursor = payload.get('cursor')
        if cursor is not None and (type(cursor) is not str or len(cursor) != 64):
            raise ValueError('Invalid public query cursor')
        started, observed_started, reason = self._clock(), time.monotonic(), None
        deadline = started + limits.timeout_seconds
        acquired, snapshot = False, None

        def interrupted():
            nonlocal reason
            if reason is None:
                if cancel is not None and cancel():
                    reason = 'cancelled'
                elif self._clock() >= deadline:
                    reason = 'deadline_exceeded'
            return reason is not None

        def check():
            if interrupted():
                raise _RequestStopped(reason)

        def remaining():
            check()
            seconds = deadline - self._clock()
            if seconds <= 0:
                raise _RequestStopped('deadline_exceeded')
            return replace(limits, timeout_seconds=seconds)

        # Before storage, require an envelope large enough for stopped setup as
        # well as the captured-identity envelope used by SQLSnapshot.query.
        envelope = self._aborted('snapshot_session_capacity_exhausted', 999.99999999999999)
        for key in ('generation', 'repository_identity', 'source_identity', 'analyzer_identity', 'config_identity'):
            envelope[key] = '0' * 64
        envelope['storage_progress_callbacks'] = 2 ** 64
        envelope['snapshot_copy_seconds'] = 999.99999999999999
        if len(encoded(envelope)) > limits.max_response_bytes:
            raise ValueError('Query byte limit cannot fit the minimum envelope')
        try:
            while True:
                check()
                if self._lock.acquire(timeout=.01):
                    acquired = True
                    break
            check()
            if self._closed:
                raise ValueError('Closed query sessions')
            for session in list(self._sessions):
                if session[2] <= self._clock():
                    self._dispose(session, check)
                    self._sessions.remove(session)
            check()
            with SourceRoot(self.output) as boundary:
                if boundary.identity != self.owner:
                    raise ValueError('Query output owner changed')
                token = _artifact_token(boundary)
            check()
            if cursor is not None:
                session = next((item for item in self._sessions if cursor in item[0]._continuations), None)
                if session is None:
                    raise ValueError('Unknown, expired, consumed or foreign session cursor')
                if arguments['operation'] == 'impact' and session[1].get('impact_token', session[1].get('token')) != token:
                    cache, current = {}, None
                    try:
                        current = SQLSnapshot(self.output, self.repository_identity, self.owner,
                            clock=self._clock, limits=remaining(), cancel=interrupted, cache=cache)
                        prior = session[0]
                        if (current.generation, current.source_identity, current.analyzer_identity, current.config_identity,
                                current.impact_receipt) != (prior.generation, prior.source_identity, prior.analyzer_identity,
                                prior.config_identity, prior.impact_receipt):
                            raise ValueError('Impact continuation is stale after source or captured commit publication')
                        session[1]['impact_token'] = token
                    finally:
                        if current is not None:
                            self._dispose([current, cache], check)
                        elif cache.get('temporary') is not None:
                            cache['temporary'].cleanup()
                            cache.clear()
            else:
                if sum(len(item[0]._continuations) for item in self._sessions) >= 32:
                    raise _RequestStopped('continuation_capacity_exhausted')
                session = next((item for item in self._sessions if item[1].get('token') == token and
                    item[1].get('owner') == self.owner), None)
                if session is None:
                    if len(self._sessions) >= 4:
                        disposable = next((item for item in self._sessions if not item[0]._continuations), None)
                        if disposable is None:
                            raise _RequestStopped('snapshot_session_capacity_exhausted')
                        self._dispose(disposable, check)
                        self._sessions.remove(disposable)
                    cache = {}
                    try:
                        snapshot = SQLSnapshot(self.output, self.repository_identity, self.owner,
                            clock=self._clock, limits=remaining(), cancel=interrupted, cache=cache)
                    except BaseException:
                        # An interrupted constructor may have admitted a private
                        # cache before metadata checks; it has no surviving reader.
                        if cache.get('temporary') is not None:
                            cache['temporary'].cleanup()
                        cache.clear()
                        raise
                    session = [snapshot, cache, self._clock() + 60]
                    self._sessions.append(session)
            fresh_snapshot = snapshot is not None
            snapshot = session[0]
            setup = (time.monotonic() - observed_started, snapshot.snapshot_copy_seconds if fresh_snapshot else 0.0)
            result = snapshot.query(**arguments, cursor=cursor, limits=remaining(), cancel=interrupted, _setup=setup)
            if reason is not None and result['stop_reason'] == 'cancelled':
                result['stop_reason'] = reason
            if len(encoded(result)) > limits.max_response_bytes:
                raise ValueError('Public query response exceeded its reserved envelope')
            return result
        except InterruptedError as error:
            stop = error.reason if isinstance(error, _RequestStopped) else reason
            if stop is None:
                if str(error).startswith(('cancelled', 'deadline_exceeded')):
                    stop = str(error).split(' ', 1)[0]
                else:
                    raise
            result = self._aborted(stop, time.monotonic() - observed_started, snapshot)
            if len(encoded(result)) > limits.max_response_bytes:
                raise ValueError('Stopped query response exceeded its reserved envelope')
            return result
        finally:
            if acquired:
                self._lock.release()


if __name__ == '__main__':
    _selfcheck()
