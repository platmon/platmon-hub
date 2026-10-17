"""ADR-0004 stage 1: commit sequence, row versions and handoff loss accounting."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub.history.projector import EventRecord
from hub.history.schema import VERSION
from hub.history.store import HistoryStore
from test_backend import NODE_ID, INSTANCE
from test_replay import document, page

QUOTA = 64 * 1024**2


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'history.db'
        self.store = HistoryStore(self.path).open()
        self.addCleanup(self.store.close)
        self.c = self.store.connection

    def event(self, received):
        self.store._insert_event(EventRecord.create(NODE_ID, 'core_state', received, None, 'live', 'core_current'))

    def seqs(self, table):
        return [r[0] for r in self.c.execute('SELECT handoff_seq FROM ' + table + ' ORDER BY rowid')]

    def losses(self):
        return self.c.execute('SELECT table_name, reason, rows, from_seq, to_seq, from_received_ms, to_received_ms, '
                              'handoff_seq FROM handoff_losses ORDER BY handoff_seq').fetchall()

    def ack(self, seq):
        self.c.execute('UPDATE handoff_state SET acked_seq=?, delivered_seq=max(delivered_seq, ?)', (seq, seq))

    def test_disabled_numbers_nothing_and_records_no_loss(self):
        self.store.insert_raw_page(page(document((1, 2)), received=1000), QUOTA)
        self.event(1000)
        self.assertEqual(self.seqs('raw_publications'), [None, None])
        self.store.prune_raw(2000)
        self.store.prune(2000)
        self.assertEqual(self.losses(), [])
        state = self.store.handoff_state()
        self.assertFalse(state['enabled'])
        self.assertEqual(len(state['store_id']), 32)

    def test_now_excludes_existing_rows_and_numbers_new_versions_once(self):
        self.store.insert_raw_page(page(document((1,)), received=1000), QUOTA)
        self.event(1000)
        self.store.enable_handoff('now', 1500)
        excluded = {row[0]: row for row in self.losses()}
        self.assertEqual(excluded['raw_publications'][1:3], ('not_handed_off', 1))
        self.assertEqual(excluded['events'][5:7], (1000, 1000))
        self.assertIn('hub_sessions', excluded)
        before = self.store.handoff_state()['next_seq']
        self.store.insert_raw_page(page(document((2, 3), after=1), after=1, received=2000), QUOTA)
        self.event(2000)
        raw, events = self.seqs('raw_publications'), self.seqs('events')
        self.assertEqual(raw[0], None)
        self.assertEqual(raw[1:], [before + 1, before + 2])
        self.assertEqual(events[1], before + 3)
        # A second enable is a no-op; disabling stops numbering, re-enabling covers only the gap.
        self.store.enable_handoff('now', 1600)
        self.store.disable_handoff()
        self.event(3000)
        self.assertIsNone(self.seqs('events')[-1])
        self.store.enable_handoff('now', 4000)
        # The unacknowledged not_handed_off loss is extended: 1000 before the first enable, 3000 while disabled.
        reenabled = [row for row in self.losses() if row[0] == 'events']
        self.assertEqual(len(reenabled), 1)
        self.assertEqual(reenabled[-1][2], 2)
        self.assertEqual(reenabled[-1][5:7], (1000, 3000))

    def test_changes_consumers_must_see_get_new_versions(self):
        self.store.enable_handoff('now', 0)
        gap = self.store._gap(NODE_ID, 'core', INSTANCE, 100, None, 'unobserved')
        self.assertEqual(len(self.c.execute('SELECT gap_uid FROM gaps WHERE id=?', (gap,)).fetchone()[0]), 32)
        first = self.c.execute('SELECT handoff_seq FROM gaps WHERE id=?', (gap,)).fetchone()[0]
        self.c.execute('UPDATE gaps SET end_ms=200 WHERE id=?', (gap,))
        second = self.c.execute('SELECT handoff_seq FROM gaps WHERE id=?', (gap,)).fetchone()[0]
        self.assertGreater(second, first)
        self.c.execute('UPDATE gaps SET end_ms=200 WHERE id=?', (gap,))
        self.assertEqual(self.c.execute('SELECT handoff_seq FROM gaps WHERE id=?', (gap,)).fetchone()[0], second)
        # A replay loss whose range grows is a new version.
        self.store.insert_raw_page(page(document((3,), latest=3)), QUOTA)
        loss = self.seqs('replay_losses')[0]
        self.c.execute('UPDATE replay_losses SET to_id=5')
        self.assertGreater(self.seqs('replay_losses')[0], loss)

    def test_session_heartbeat_renumbers_once_per_delivered_segment(self):
        self.store.enable_handoff('now', 0)
        session = self.store.session_id
        self.c.execute('INSERT INTO hub_sessions (session_id,started_at_ms,last_seen_ms) VALUES (?,1,1)', ('s2',))
        seq = lambda: self.c.execute('SELECT handoff_seq FROM hub_sessions WHERE session_id=?', ('s2',)).fetchone()[0]
        first = seq()
        self.c.execute("UPDATE hub_sessions SET last_seen_ms=2 WHERE session_id='s2'")
        self.assertEqual(seq(), first)
        self.c.execute('UPDATE handoff_state SET delivered_seq=?', (first,))
        self.c.execute("UPDATE hub_sessions SET last_seen_ms=3 WHERE session_id='s2'")
        renumbered = seq()
        self.assertGreater(renumbered, first)
        self.c.execute("UPDATE hub_sessions SET last_seen_ms=4 WHERE session_id='s2'")
        self.assertEqual(seq(), renumbered)
        self.c.execute("UPDATE hub_sessions SET closed_at_ms=5 WHERE session_id='s2'")
        self.assertGreater(seq(), renumbered)
        self.assertIsNotNone(session)

    def test_retention_of_unacknowledged_rows_records_coalesced_losses(self):
        self.store.enable_handoff('now', 0)
        for received in (1000, 1100, 1200):
            self.event(received)
        self.store.insert_raw_page(page(document((1, 2)), received=1000), QUOTA)
        self.store.insert_raw_page(page(document((3,), after=2), after=2, received=1100), QUOTA)
        self.ack(self.seqs('events')[0])
        self.store.prune(1050)
        self.store.prune(1150)
        self.assertEqual(self.store.prune_raw(1050), 2)
        self.assertEqual(self.store.prune_raw(1150), 1)
        retention = {row[0]: row for row in self.losses() if row[1] == 'retention'}
        # The acknowledged first event is no loss; the second, deleted later, is one row.
        self.assertEqual(retention['events'][2], 1)
        self.assertEqual(retention['events'][5:7], (1100, 1100))
        self.assertEqual(retention['raw_publications'][2], 3)
        self.assertEqual(retention['raw_publications'][5:7], (1000, 1100))
        self.assertEqual(len([row for row in self.losses() if row[:2] == ('raw_publications', 'retention')]), 1)

    def test_quota_evicts_acknowledged_rows_before_older_unacknowledged_ones(self):
        self.store.enable_handoff('now', 0)
        self.store.insert_raw_page(page(document((1,)), received=5000), QUOTA)
        self.store.insert_raw_page(page(document((1,), stream='wifi'), received=1000), QUOTA)
        acked = self.c.execute("SELECT handoff_seq FROM raw_publications WHERE stream='core'").fetchone()[0]
        self.ack(acked)
        self.assertEqual(self.store.evict_raw_over_quota(0, limit=1), 1)
        self.assertEqual([r[0] for r in self.c.execute('SELECT stream FROM raw_publications')], ['wifi'])
        self.assertEqual([row for row in self.losses() if row[1] == 'quota'], [])
        self.assertEqual(self.store.evict_raw_over_quota(0, limit=1), 1)
        quota = [row for row in self.losses() if row[1] == 'quota']
        self.assertEqual([(row[0], row[2]) for row in quota], [('raw_publications', 1)])

    def test_existing_start_records_deletion_of_rows_awaiting_backfill(self):
        self.store.insert_raw_page(page(document((1,)), received=1000), QUOTA)
        self.store.enable_handoff('existing', 1500)
        self.assertEqual([row for row in self.losses() if row[1] == 'not_handed_off'], [])
        self.store.prune_raw(1200)
        row = [row for row in self.losses() if row[1] == 'retention'][0]
        self.assertEqual((row[0], row[2], row[3], row[5]), ('raw_publications', 1, None, 1000))

    def test_loss_rows_are_versions_and_stay_until_acknowledged(self):
        self.store.enable_handoff('now', 0)
        self.event(1000)
        self.store.prune(2000)
        loss = self.losses()[-1]
        self.assertIsNotNone(loss[7])
        self.store.prune(10**12)
        self.assertIn(loss[:3], [row[:3] for row in self.losses()])
        self.ack(self.store.handoff_state()['next_seq'])
        self.store.prune(10**12)
        self.assertEqual([row for row in self.losses() if row[0] == 'events'], [])


class MigrationTests(unittest.TestCase):
    def test_v8_gaps_get_uids_and_store_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.db'
            with patch('hub.history.store.MIGRATION_9', 'INVALID SQL;'):
                with self.assertRaises(sqlite3.Error): HistoryStore(path).open()
            c = sqlite3.connect(path)
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 8)
            c.execute("INSERT INTO gaps (node_id,scope,start_ms,reason) VALUES (?, 'core', 1, 'unobserved')", (NODE_ID,))
            c.commit()
            c.close()
            store = HistoryStore(path).open()
            try:
                c = store.connection
                self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], VERSION)
                self.assertEqual(len(list(Path(directory).glob('history.db.v8-backup-*'))), 1)
                self.assertEqual(len(c.execute('SELECT gap_uid FROM gaps').fetchone()[0]), 32)
                self.assertIsNone(c.execute('SELECT handoff_seq FROM gaps').fetchone()[0])
                self.assertFalse(store.handoff_state()['enabled'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
