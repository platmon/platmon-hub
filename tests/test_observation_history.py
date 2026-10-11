import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub.history.projector import ContinuityRecord, project_observation
from hub.history.schema import SQL, VERSION
from hub.history.store import HistoryStore
from test_backend import NODE_ID
from test_history import record


class ObservationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'history.db'
        self.store = HistoryStore(self.path).open()
        self.addCleanup(self.store.close)

    def group(self, identity=1, connected=None):
        return dict(state='ok', stale_after_ms=30000,
                    observation=dict(id=identity, data_age_ms=10),
                    data=dict(interfaces=[dict(name='wlan0', connected=connected, signal_dbm=None, secret='excluded')]))

    def test_group_identity_null_and_bounded_projection(self):
        first = project_observation(NODE_ID, 'a', 'wifi', self.group(), 1000)
        self.assertTrue(first.valid())
        self.assertIsNone(json.loads(first.data_json)['interfaces'][0]['connected'])
        self.assertNotIn('secret', first.data_json)
        self.assertEqual(self.store.insert_batch([first, first])['duplicates'], 1)
        other = project_observation(NODE_ID, 'b', 'wifi', self.group(), 2000)
        self.assertEqual(self.store.insert_batch([other])['persisted'], 1)
        large = self.group()
        large['data']['interfaces'][0]['name'] = 'x' * 20000
        self.assertFalse(project_observation(NODE_ID, 'a', 'wifi', large, 1000).valid())
        self.assertIsNone(project_observation(NODE_ID, 'a', 'wifi', dict(self.group(), state='stale'), 1000))

    def test_failure_recovery_restart_and_optional_scope(self):
        rows = [ContinuityRecord(NODE_ID, 'core', 'a', 1000, None),
                ContinuityRecord(NODE_ID, 'wifi', 'a', 1100, 'stale'),
                ContinuityRecord(NODE_ID, 'core', 'a', 2000, 'offline'),
                ContinuityRecord(NODE_ID, 'core', 'a', 3000, 'offline'),
                ContinuityRecord(NODE_ID, 'core', 'a', 4000, None),
                ContinuityRecord(NODE_ID, 'core', 'b', 5000, None)]
        self.store.insert_batch(rows)
        gaps = self.store.connection.execute('SELECT scope,start_ms,end_ms,reason FROM gaps ORDER BY id').fetchall()
        self.assertEqual(gaps, [('wifi', 1100, None, 'stale'), ('core', 2000, 4000, 'offline'), ('core', 4000, 5000, 'agent_restart')])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM core_samples').fetchone()[0], 0)

    def test_hub_restart_marks_unobserved_not_offline(self):
        self.store.insert_batch([ContinuityRecord(NODE_ID, 'core', 'a', 1000, None)])
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.connection.execute('SELECT reason FROM gaps').fetchone()[0], 'unobserved')

    def test_v1_migration_and_backup(self):
        self.store.close()
        old = self.path.with_name('v1.db')
        c = sqlite3.connect(str(old))
        c.executescript(SQL)
        c.execute('INSERT INTO core_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', record().values()[:12])
        c.commit()
        c.close()
        migrated = HistoryStore(old).open()
        try:
            self.assertEqual(migrated.connection.execute('PRAGMA user_version').fetchone()[0], VERSION)
            self.assertEqual(migrated.connection.execute('SELECT count(*) FROM core_samples').fetchone()[0], 1)
        finally:
            migrated.close()
        backup = next(old.parent.glob('v1.db.v1-backup-*'))
        c = sqlite3.connect(str(backup))
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
        c.close()

    def test_failed_upgrade_preserves_v1(self):
        old = self.path.with_name('v1-fail.db')
        c = sqlite3.connect(str(old))
        c.executescript(SQL)
        c.close()
        with patch('hub.history.store.MIGRATION_2', 'CREATE TABLE partial(x); BAD SQL;'):
            with self.assertRaises(sqlite3.Error):
                HistoryStore(old).open()
        c = sqlite3.connect(str(old))
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
        c.close()
