import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hub.history.projector import CoreRecord
from hub.history.store import HistoryStore, SchemaError, read_connection
from hub.history.writer import HistoryWriter
from hub.registry import Node
from hub.runtime import Fleet
from hub.state import NodeState
from test_backend import NODE_ID, st, stats, FakeClient


def record(seq=1, instance='a'):
    return CoreRecord(NODE_ID, instance, seq, int(time.time() * 1000), 1, 5000,
                      'ready', 25.0, None, 50.0, None, None)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError('condition not reached before deadline')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'history space.db'
        self.store = HistoryStore(self.path).open()
        self.addCleanup(self.store.close)

    def test_identity_restart_null_and_regression(self):
        self.assertEqual(self.store.insert_batch([record(2)])['persisted'], 1)
        self.store.close()
        self.store.open()
        counts = self.store.insert_batch([record(2), record(1), record(1, 'b')])
        self.assertEqual(counts, dict(persisted=1, duplicates=1, dropped_regression=1))
        c = read_connection(self.path)
        try:
            self.assertEqual(c.execute('SELECT count(*) FROM core_samples').fetchone()[0], 2)
            self.assertIsNone(c.execute('SELECT gpu_percent FROM core_samples LIMIT 1').fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                c.execute('DELETE FROM core_samples')
        finally:
            c.close()

    def test_lock_rollback_retention_and_ownership(self):
        other = HistoryStore(self.path)
        with self.assertRaises(OSError):
            other.open()
        c = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            c.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                self.store.insert_batch([record()])
            c.execute('ROLLBACK')
        finally:
            c.close()
        self.store.insert_batch([record()])
        self.assertEqual(self.store.prune(0), 0)
        self.assertEqual(self.store.prune(2**63 - 1), 1)
        self.store.close()
        # All handles must be released on Windows too.
        moved = self.path.with_suffix('.moved')
        self.path.rename(moved)
        moved.rename(self.path)
        other.open()
        other.close()

    def test_batch_failure_is_atomic(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.insert_batch([record(), replace(record(2), core_state='bad')])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM core_samples').fetchone()[0], 0)

    def test_unknown_schema_preserved(self):
        self.store.connection.execute('PRAGMA user_version=99')
        self.store.close()
        with self.assertRaises(SchemaError):
            self.store.open()
        c = sqlite3.connect(str(self.path))
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 99)
        c.close()

    def test_initial_migration_rolls_back(self):
        self.store.close()
        path = self.path.with_name('broken.db')
        with patch('hub.history.store.SQL', 'CREATE TABLE marker (x); INVALID SQL;'):
            with self.assertRaises(sqlite3.Error):
                HistoryStore(path).open()
        c = sqlite3.connect(str(path))
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        c.close()


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'history.db'

    def test_queue_overflow_and_shutdown_persistence(self):
        writer = HistoryWriter(self.path, capacity=1)
        self.assertTrue(writer.submit(record()))
        self.assertFalse(writer.submit(record(2)))
        self.assertFalse(writer.submit(replace(record(), sequence=2**64)))
        writer.start()
        self.assertTrue(writer.close()['flushed'])
        self.assertFalse(writer.submit(record(3)))
        state = writer.snapshot()
        self.assertEqual(state['persisted'], 1)
        self.assertEqual(state['dropped_queue_full'], 1)
        self.assertEqual(state['dropped_invalid'], 1)
        self.assertEqual(state['dropped_closed'], 1)
        c = read_connection(self.path)
        self.assertEqual(c.execute('SELECT count(*) FROM core_samples').fetchone()[0], 1)
        c.close()

    def test_storage_fault_does_not_stop_polling(self):
        class Broken(HistoryStore):
            def insert_batch(self, records):
                raise sqlite3.OperationalError('disk full')
        writer = HistoryWriter(self.path, store_factory=Broken, retry_seconds=0.05)
        writer.start()
        client = FakeClient()
        client.handlers = {'/api/status': st(), '/api/stats': stats()}
        fleet = Fleet([Node(NODE_ID, 'test', 'http://192.0.2.1:9797')], client=client, history=writer)
        try:
            fleet.core_once(NODE_ID)
            wait_for(lambda: writer.snapshot()['dropped_storage_error'] == 1)
            fleet.core_once(NODE_ID)
            self.assertEqual(fleet.get_node(NODE_ID)['headline'], 'live')
            self.assertEqual(writer.snapshot()['submitted'], 1)
        finally:
            fleet.close()

    def test_unavailable_database_and_bounded_close(self):
        writer = HistoryWriter(self.path / 'missing.db', retry_seconds=0.05)
        writer.submit(record())
        writer.start()
        wait_for(lambda: writer.snapshot()['dropped_storage_error'] == 1)
        self.assertFalse(writer.close()['flushed'])
        self.assertEqual(writer.snapshot()['queue_depth'], 0)

    def test_acceptance_and_submission_outside_state_lock(self):
        node = Node(NODE_ID, 'test', 'http://192.0.2.1:9797')
        state = NodeState(node)
        self.assertEqual(state.accept_core(st(), stats()).disposition, 'new')
        self.assertEqual(state.accept_core(st(), stats()).disposition, 'duplicate')
        self.assertEqual(state.accept_core(st(), stats(seq=1)).disposition, 'rejected')
        client = FakeClient()
        client.handlers = {'/api/status': st(), '/api/stats': stats()}
        fleet = Fleet([node], client=client)
        acquired = threading.Event()
        class Sink:
            def submit(self, value):
                def check():
                    with fleet.nodes[NODE_ID]._lock:
                        acquired.set()
                thread = threading.Thread(target=check)
                thread.start()
                self_ok = acquired.wait(1)
                thread.join(1)
                if not self_ok:
                    raise AssertionError('state lock held at submission')
            def close(self): pass
        fleet.history = Sink()
        try:
            fleet.core_once(NODE_ID)
            self.assertTrue(acquired.is_set())
        finally:
            fleet.close()

    def test_clock_jump_suspends_retention(self):
        class Clock:
            value = time.time()
            def __call__(self): return self.value
        wall = Clock()
        writer = HistoryWriter(self.path, wall=wall)
        writer.start()
        try:
            wait_for(lambda: writer.snapshot()['last_retention_ms'] is not None)
            wall.value += 86400
            wait_for(lambda: writer.snapshot()['clock_discontinuity'])
            writer.submit(record())
            wait_for(lambda: writer.snapshot()['persisted'] == 1)
        finally:
            writer.close()

    def test_process_crash_preserves_committed_rows_and_releases_lock(self):
        import subprocess
        import sys
        script = '''
import os, sys
from hub.history.store import HistoryStore
from hub.history.projector import CoreRecord
s = HistoryStore(sys.argv[1]).open()
r = CoreRecord('00000000-0000-0000-0000-000000000001', 'a', 1, 1000, 0, 5000, 'ready', None, None, None, None, None)
s.insert_batch([r])
s.connection.execute('BEGIN IMMEDIATE')
s.connection.execute('UPDATE core_samples SET cpu_percent=99')
os._exit(0)
'''
        subprocess.run([sys.executable, '-c', script, str(self.path)], check=True, timeout=10)
        store = HistoryStore(self.path).open()
        try:
            self.assertEqual(store.connection.execute('SELECT count(*) FROM core_samples').fetchone()[0], 1)
            self.assertIsNone(store.connection.execute('SELECT cpu_percent FROM core_samples').fetchone()[0])
        finally:
            store.close()

    def test_reader_does_not_block_commit(self):
        store = HistoryStore(self.path).open()
        try:
            store.insert_batch([record()])
            c = read_connection(self.path)
            try:
                c.execute('BEGIN')
                self.assertEqual(c.execute('SELECT count(*) FROM core_samples').fetchone()[0], 1)
                self.assertEqual(store.insert_batch([record(2)])['persisted'], 1)
                self.assertEqual(c.execute('SELECT count(*) FROM core_samples').fetchone()[0], 1)
            finally:
                c.close()
        finally:
            store.close()
