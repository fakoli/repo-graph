"""Finite query candidate over one immutable set of collected facts.

This component experiment is not a production index or an engine selection.
Adjacency indexes contain fact IDs; they do not infer targets or copy bodies.
"""
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import secrets
import time


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@dataclass(frozen=True)
class Limits:
    max_entities: int = 50
    max_edges: int = 100
    max_examined_relationships: int = 1000
    max_response_bytes: int = 32768
    timeout_seconds: float = 1.0

    def __post_init__(self):
        for field, ceiling in (('max_entities', 256), ('max_edges', 256),
                               ('max_examined_relationships', 100000), ('max_response_bytes', 1048576)):
            value = getattr(self, field)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError('Invalid query limit: ' + field)
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 30:
            raise ValueError('Invalid query deadline')


class Snapshot:
    """Owned finite snapshot; saved continuations expire and bind every filter.

    ponytail: in-memory component snapshot capped at 64 MiB/40000 facts;
    qualify the existing SQLite index before large-corpus product integration.
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
            if site['role'] not in ('call', 'reference') or site['certainty'] not in ('resolved', 'candidate', 'unresolved'):
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
            'role': role, 'order': 'physical-occurrence-v1'})).hexdigest()
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
                    'returned_entities': len(entities), 'returned_edges': len(rows),
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
            if examined >= limits.max_examined_relationships:
                reason = 'work_budget_exceeded'; break
            if len(rows) >= limits.max_edges:
                reason = 'edge_budget_exceeded'; break
            key = relations[offset]
            examined += 1  # Before scope/name/role rejection, never a post-traversal trim.
            site = self._sites[key[0]]
            target = site['caller'] if operation in ('callers', 'impact') else key[1]
            if (role != 'all' and site['role'] != role or not site['path'].startswith(scope) or
                    prefix and (target is None or not self._definitions[target]['name'].startswith(prefix))):
                frontier[0][2] += 1; continue
            candidate_entities = entities | ({target} if target is not None and target != seed else set())
            if len(candidate_entities) > limits.max_entities:
                reason = 'entity_budget_exceeded'; break
            row = self._row(key)
            previous_entities = entities
            entities = candidate_entities
            state['matched'] += 1
            rows.append(row)
            if len(encoded(response('0' * 64, 'response_byte_budget_exceeded'))) > limits.max_response_bytes:
                rows.pop(); entities = previous_entities; state['matched'] -= 1
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
