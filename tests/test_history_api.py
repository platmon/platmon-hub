import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hub.history.projector import ContinuityRecord
from hub.history.query import parameters, query_history, QueryError
from hub.history.store import HistoryStore
from test_backend import NODE_ID
import test_backend as backend
from test_history import record


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'history.db'
        self.store = HistoryStore(self.path).open()
        self.addCleanup(self.store.close)

    def query(self, maximum=1200, metric='cpu_percent'):
        return query_history(self.path, NODE_ID, metric, 0, 100000, maximum)

    def test_empty_and_parameter_bounds(self):
        self.assertEqual(self.query()['points'], [])
        for query in ('', 'metric=drop&from_ms=0&to_ms=1', 'metric=cpu_percent&from_ms=2&to_ms=1',
                      'metric=cpu_percent&from_ms=0&to_ms=604800001',
                      'metric=cpu_percent&from_ms=0&to_ms=1&max_points=1201',
                      'metric=cpu_percent&from_ms=0&to_ms=1&max_points=0',
                      'metric=cpu_percent&from_ms=0&from_ms=1&to_ms=2'):
            with self.subTest(query=query), self.assertRaises(QueryError):
                parameters(query)

    def test_downsampling_retains_spike_count_and_null(self):
        self.store.insert_batch([replace(record(i+1), received_at_ms=i*100, cpu_percent=100 if i == 10 else 0) for i in range(100)])
        points = self.query(1)['points']
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['count'], 100)
        self.assertAlmostEqual(points[0]['average'], 1)
        self.assertEqual(points[0]['maximum'], 100)
        self.assertEqual(points[0]['minimum'], 0)
        null = self.query(1, 'gpu_percent')['points'][0]
        self.assertEqual(null['count'], 0)
        self.assertIsNone(null['average'])

    def test_restart_null_and_gap_segments_never_join(self):
        self.store.insert_batch([replace(record(), received_at_ms=100),
                                 ContinuityRecord(NODE_ID, 'core', 'a', 200, 'offline'),
                                 ContinuityRecord(NODE_ID, 'core', 'a', 300, None),
                                 replace(record(2), received_at_ms=400),
                                 replace(record(3), received_at_ms=500, cpu_percent=None),
                                 replace(record(1, 'b'), received_at_ms=600)])
        result = self.query()
        self.assertEqual(len({p['segment'] for p in result['points']}), 4)
        self.assertEqual(result['gaps'][0]['reason'], 'offline')
        with self.assertRaises(QueryError) as error:
            self.query(1)
        self.assertEqual(error.exception.code, 'history_range_too_dense')

    def test_bounded_large_query_and_deadline(self):
        self.store.insert_batch([replace(record(i+1), received_at_ms=i) for i in range(10000)])
        self.assertLessEqual(len(self.query(20)['points']), 20)
        with self.assertRaises(QueryError):
            query_history(self.path, NODE_ID, 'cpu_percent', 0, 100000, 20, budget_seconds=0)


class HistoryHttpTests(unittest.TestCase):
    setUp = backend.HttpTests.setUp
    tearDown = backend.HttpTests.tearDown
    request = backend.HttpTests.request
    def test_history_disabled_unknown_node_and_static_query(self):
        query = '?metric=cpu_percent&from_ms=0&to_ms=1000'
        self.assertEqual(self.request('GET', '/api/nodes/' + NODE_ID + '/history' + query)[0], 503)
        self.assertEqual(self.request('GET', '/api/nodes/missing/history' + query)[0], 404)
        self.assertEqual(self.request('GET', '/api/nodes/' + NODE_ID + '/history?metric=bad')[0], 400)
        self.assertEqual(self.request('GET', '/assets/app.js?x=1')[0], 404)

    def test_read_history_over_http(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.db'
            store = HistoryStore(path).open()
            try:
                store.insert_batch([replace(record(), received_at_ms=500)])
                self.fleet.history = SimpleNamespace(path=path, snapshot=lambda: {'schema_version': 2}, close=lambda: None)
                status, body = self.request('GET', '/api/nodes/' + NODE_ID + '/history?metric=cpu_percent&from_ms=0&to_ms=1000')
                self.assertEqual(status, 200)
                self.assertEqual(body['node_id'], NODE_ID)
                self.assertEqual(body['points'][0]['average'], 25)
            finally:
                self.fleet.history = None
                store.close()
