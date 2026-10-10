import tempfile
import unittest
from pathlib import Path
from hub.history.events import query_events
from hub.history.projector import ContinuityRecord, EventRecord
from hub.history.query import QueryError
from hub.history.store import HistoryStore
from test_backend import NODE_ID


class EventTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'history.db'
        self.store = HistoryStore(self.path).open()
        self.addCleanup(self.store.close)

    def test_transitions_not_repeated_offline_and_restart(self):
        self.store.insert_batch([
            ContinuityRecord(NODE_ID, 'core', 'a', 1000, None),
            ContinuityRecord(NODE_ID, 'core', 'a', 2000, 'stale'),
            ContinuityRecord(NODE_ID, 'core', 'a', 3000, 'offline'),
            ContinuityRecord(NODE_ID, 'core', 'a', 4000, 'offline'),
            ContinuityRecord(NODE_ID, 'core', 'b', 5000, None),
            ContinuityRecord(NODE_ID, 'wifi', 'a', 1000, 'error'),
            ContinuityRecord(NODE_ID, 'wifi', 'a', 2000, 'error'),
            ContinuityRecord(NODE_ID, 'wifi', 'a', 3000, None)])
        result = query_events(self.path, 'from_ms=0&to_ms=6000')['events']
        self.assertEqual(sum(e['current_state']=='offline' for e in result), 1)
        self.assertEqual(sum(e['event_type']=='agent_restart' for e in result), 1)
        self.assertEqual(sum(e['event_type']=='optional_error' for e in result), 1)
        self.assertEqual(sum(e['event_type']=='optional_recovered' for e in result), 1)

    def test_idempotency_filters_pagination_and_retention(self):
        event = EventRecord.create(NODE_ID, 'registry_changed', 1000, 'enabled', 'disabled', 'enabled_changed')
        self.store.insert_batch([event, event])
        self.store.insert_batch([EventRecord.create(None, 'history_writer_error', 1000, 'healthy', 'degraded', 'OperationalError')])
        first = query_events(self.path, 'from_ms=0&to_ms=2000&limit=1')
        self.assertIsNotNone(first['next_cursor'])
        second = query_events(self.path, 'from_ms=0&to_ms=2000&limit=1&cursor='+first['next_cursor'])
        self.assertNotEqual(first['events'][0]['event_id'], second['events'][0]['event_id'])
        self.assertEqual(len(query_events(self.path, 'from_ms=0&to_ms=2000&type=registry_changed&node_id='+NODE_ID)['events']), 1)
        self.store.prune(2000, event_cutoff_ms=500)
        self.assertEqual(len(query_events(self.path, 'from_ms=0&to_ms=2000')['events']), 2)
        self.store.prune(2000, event_cutoff_ms=2000)
        self.assertEqual(query_events(self.path, 'from_ms=0&to_ms=2000')['events'], [])

    def test_reject_bad_filters(self):
        for query in ('type=arbitrary', 'limit=0', 'node_id=bad', 'from_ms=1&to_ms=0', 'cursor=bad', 'unknown=1'):
            with self.subTest(query=query), self.assertRaises(QueryError): query_events(self.path, query)

    def test_writer_fault_and_recovery_are_recorded(self):
        import sqlite3
        import threading
        from hub.history.writer import HistoryWriter
        from test_history import record, wait_for
        self.store.close()
        fault, failed = threading.Event(), threading.Event()
        fault.set()
        class Flaky(HistoryStore):
            def insert_batch(self, records):
                if fault.is_set():
                    failed.set()
                    raise sqlite3.OperationalError('injected')
                return super().insert_batch(records)
        writer = HistoryWriter(self.path, store_factory=Flaky)
        writer.submit(record())
        writer.start()
        try:
            self.assertTrue(failed.wait(3))
            fault.clear()
            wait_for(lambda: writer.snapshot()['persisted'] >= 3)
        finally:
            fault.clear()
            writer.close()
        types = [event['event_type'] for event in query_events(self.path, '')['events']]
        self.assertEqual(types.count('history_writer_error'), 1)
        self.assertEqual(types.count('history_writer_recovered'), 1)
