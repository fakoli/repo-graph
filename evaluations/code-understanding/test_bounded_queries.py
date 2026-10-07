"""Finite control-flow regressions; not structural or scale qualification."""
import unittest
from evaluations.bounded_queries import Limits, Snapshot, encoded


def facts():
    def handle(identifier, offset):
        return {'id': identifier, 'path': 'synthetic.py',
                'range': {'start_byte': offset, 'end_byte': offset + 1, 'start_line': 1, 'end_line': 1},
                'provenance': {'source_sha256': 'a' * 64}}
    definitions = [dict(handle(name, index), name=name) for index, name in enumerate(['hub'] + ['leaf_%03d' % i for i in range(113)])]
    sites = [dict(handle('s%03d' % i, 1000 + i), role='call', caller='hub', targets=['leaf_%03d' % i],
                  certainty='resolved', targets_exhaustive=True) for i in range(113)]
    return {'definitions': definitions, 'sites': sites}


class QueryChecks(unittest.TestCase):
    def test_pages_preserve_occurrences_without_extra_relationship_work(self):
        snapshot = Snapshot(facts(), 'a' * 64, 'b' * 64)
        cursor, sizes, rows, work = None, [], [], 0
        for _ in range(8):
            result = snapshot.query('hub', cursor=cursor, limits=Limits(max_edges=17, max_examined_relationships=20))
            sizes.append(len(result['rows'])); rows.extend(result['rows']); work += result['examined_relationships']
            cursor = result['cursor']
            if cursor is None:
                break
        self.assertEqual(sizes, [17] * 6 + [11])
        self.assertEqual(work, 113)
        self.assertEqual([row['site']['id'] for row in rows], ['s%03d' % i for i in range(113)])
        self.assertEqual(result['total_count'], {'value': 113, 'kind': 'exact'})

    def test_filters_consume_work_and_counts_remain_qualified(self):
        snapshot = Snapshot(facts(), 'a' * 64, 'b' * 64)
        result = snapshot.query('hub', prefix='leaf_1', limits=Limits(max_examined_relationships=7))
        self.assertEqual((result['examined_relationships'], result['rows']), (7, []))
        self.assertEqual(result['total_count'], {'value': 0, 'kind': 'lower_bound'})
        self.assertEqual(result['stop_reason'], 'work_budget_exceeded')
        result = snapshot.query('hub', prefix='leaf_1', limits=Limits(max_entities=256, max_edges=256))
        self.assertEqual(len(result['rows']), 13)
        self.assertEqual(result['examined_relationships'], 113)

    def test_byte_limit_and_entity_limit_resume_first_unreturned_row(self):
        snapshot = Snapshot(facts(), 'a' * 64, 'b' * 64)
        result = snapshot.query('hub', limits=Limits(max_response_bytes=1024))
        self.assertLessEqual(len(encoded(result)), 1024)
        self.assertLess(result['examined_relationships'], 113)
        resumed = snapshot.query('hub', cursor=result['cursor'])
        self.assertEqual(resumed['rows'][0]['site']['id'], 's%03d' % len(result['rows']))
        result = snapshot.query('hub', limits=Limits(max_entities=1))
        self.assertEqual(result['rows'], [])  # One edge carries seed and target handles.
        self.assertEqual(result['returned_symbol_handles'], 0)
        resumed = snapshot.query('hub', cursor=result['cursor'])
        self.assertEqual(resumed['rows'][0]['site']['id'], 's000')
        result = snapshot.query('hub', limits=Limits(max_entities=2))
        self.assertEqual(result['returned_symbol_handles'], 2)
        self.assertEqual(result['returned_entities'], 1)
        resumed = snapshot.query('hub', cursor=result['cursor'])
        self.assertEqual(resumed['rows'][0]['site']['id'], 's001')
        cursor, rows, work = None, [], 0
        for _ in range(32):
            page = snapshot.query('hub', cursor=cursor, limits=Limits(max_response_bytes=4096))
            self.assertLessEqual(len(encoded(page)), 4096)
            rows.extend(page['rows']); work += page['examined_relationships']; cursor = page['cursor']
            if cursor is None:
                break
        self.assertIsNone(cursor)
        self.assertEqual([r['site']['id'] for r in rows], ['s%03d' % i for i in range(113)])
        self.assertEqual(work, 113)

    def test_cancel_deadline_and_snapshot_staleness(self):
        snapshot = Snapshot(facts(), 'a' * 64, 'b' * 64)
        result = snapshot.query('hub', cancel=lambda: True)
        self.assertEqual((result['examined_relationships'], result['rows'], result['stop_reason']), (0, [], 'cancelled'))
        calls = [0]
        def cancel():
            calls[0] += 1
            return calls[0] > 3
        result = snapshot.query('hub', cancel=cancel)
        self.assertEqual((result['examined_relationships'], result['stop_reason']), (3, 'cancelled'))
        tick = [0]
        def clock():
            tick[0] += 1
            return tick[0] / 100
        timed = Snapshot(facts(), 'a' * 64, 'b' * 64, clock=clock)
        result = timed.query('hub', limits=Limits(timeout_seconds=.04))
        self.assertLessEqual(result['examined_relationships'], 3)
        self.assertEqual(result['stop_reason'], 'deadline_exceeded')
        cursor = snapshot.query('hub', limits=Limits(max_edges=1))['cursor']
        for changed in ({'prefix': 'leaf_1'}, {'scope': 'other'}, {'operation': 'callers'}, {'depth': 2}):
            with self.assertRaises(ValueError): snapshot.query('hub', cursor=cursor, **changed)
        with self.assertRaises(ValueError): Snapshot(facts(), 'c' * 64, 'b' * 64).query('hub', cursor=cursor)

    def test_cycles_and_returned_handles_cannot_mutate_snapshot(self):
        value = facts()
        value['sites'] = [dict(value['sites'][0], caller='hub', targets=['leaf_000']),
                          dict(value['sites'][1], caller='leaf_000', targets=['hub'])]
        snapshot = Snapshot(value, 'a' * 64, 'b' * 64)
        with self.assertRaises(AttributeError): snapshot.generation = 'f' * 64
        with self.assertRaises(AttributeError): snapshot.definitions
        result = snapshot.query('hub', operation='reachable', depth=4)
        self.assertEqual((result['examined_relationships'], result['returned_edges'], result['returned_entities']), (2, 2, 1))
        result['rows'][0]['target']['range']['start_byte'] = 999
        value['definitions'][1]['range']['start_byte'] = 888
        self.assertEqual(snapshot.query('hub')['rows'][0]['target']['range']['start_byte'], 1)

    def test_invalid_limits_and_expired_cursor(self):
        for kwargs in ({'max_entities': True}, {'max_edges': 257}, {'timeout_seconds': float('inf')}, {'max_response_bytes': 0}):
            with self.assertRaises(ValueError): Limits(**kwargs)
        now = [0]
        snapshot = Snapshot(facts(), 'a' * 64, 'b' * 64, clock=lambda: now[0])
        cursor = snapshot.query('hub', limits=Limits(max_edges=1))['cursor']
        now[0] = 61
        with self.assertRaises(ValueError): snapshot.query('hub', cursor=cursor)
        with self.assertRaises(ValueError): snapshot.query('hub', limits=Limits(max_response_bytes=1))
        for field, value in (('targets_exhaustive', []), ('caller', []), ('reason', [])):
            malformed = facts(); malformed['sites'][0][field] = value
            with self.assertRaises(ValueError): Snapshot(malformed, 'a' * 64, 'b' * 64)
        malformed = facts(); malformed['definitions'][0]['provenance']['source_sha256'] = []
        with self.assertRaises(ValueError): Snapshot(malformed, 'a' * 64, 'b' * 64)


if __name__ == '__main__':
    unittest.main()
