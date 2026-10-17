"""ADR-0004 stage 3: handoff inside a running Hub, acknowledgements, spool deletion and backfill."""
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from hub import handoff
from hub.__main__ import main
from hub.history.projector import EventRecord
from hub.history.store import HistoryStore
from hub.history.writer import HistoryWriter
from test_backend import NODE_ID
from test_replay import document, page

DAY = 86400000


class Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db, self.spool = root / 'history.db', root / 'spool'
        self.spool.mkdir()
        self.clock = Clock()
        # Real wall time: spool deletion compares file modification times with it.
        self.wall = [time.time()]

    def writer(self):
        writer = HistoryWriter(self.db, raw_retention_days=1, raw_quota_bytes=64 * 1024**2)
        writer.start()
        self.addCleanup(writer.close)
        return writer

    def scheduler(self, writer, **kwargs):
        options = dict(quota_bytes=64 * 1024**2, retention_ms=DAY, data_retention_ms=DAY, interval_s=900,
                       tick_s=3600, clock=self.clock, wall=lambda: self.wall[0])
        options.update(kwargs)
        scheduler = handoff.HandoffScheduler(writer, self.spool, **options).start()
        self.addCleanup(scheduler.close, False)
        return scheduler

    def received(self):
        return int(self.wall[0] * 1000)

    def ack(self, scheduler, segment, content=None):
        acks = scheduler.directory / 'acks'
        acks.mkdir(exist_ok=True)
        digest = handoff.digest_file(scheduler.directory / segment) if content is None else content
        temporary = acks / ('.' + segment + '.tmp')
        temporary.write_text(digest, encoding='ascii')
        os.replace(temporary, acks / (segment + '.ack'))

    def state(self, writer):
        return writer.handoff('handoff_state').result(15)

    def test_builds_on_interval_and_acknowledges_a_contiguous_prefix(self):
        writer = self.writer()
        scheduler = self.scheduler(writer)
        writer.submit_raw(page(document((1, 2)), received=self.received())).result(15)
        self.assertIsNone(scheduler.tick())
        self.clock.value += 900
        first = scheduler.tick()
        self.assertTrue(first['complete'])
        writer.submit_raw(page(document((3,), after=2), after=2, received=self.received())).result(15)
        second = scheduler.tick(force=True)
        self.assertEqual(second['from_seq'], first['to_seq'])
        # Acknowledging only the second segment does not move past the first.
        self.ack(scheduler, second['segment'])
        scheduler.tick()
        self.assertEqual(self.state(writer)['acked_seq'], 0)
        self.ack(scheduler, first['segment'], content='0' * 64)
        scheduler.tick()
        self.assertEqual(scheduler.snapshot()['rejected_acks'], 1)
        self.ack(scheduler, first['segment'])
        scheduler.tick()
        self.assertEqual(self.state(writer)['acked_seq'], second['to_seq'])
        self.assertEqual(scheduler.snapshot()['unacknowledged_segments'], 0)
        # Acknowledged segments go after the spool retention, with their acknowledgement.
        old = self.wall[0] - 2 * 86400
        for name in (first['segment'], second['segment']):
            os.utime(scheduler.directory / name, (old, old))
        scheduler.tick()
        self.assertEqual(handoff.segments(scheduler.directory), [])
        self.assertEqual(list((scheduler.directory / 'acks').iterdir()), [])
        self.assertEqual(scheduler.snapshot()['segments_deleted'], 2)

    def test_unacknowledged_segment_past_data_retention_becomes_a_recorded_loss(self):
        writer = self.writer()
        scheduler = self.scheduler(writer)
        writer.submit_raw(page(document((1,)), received=self.received())).result(15)
        lost = scheduler.tick(force=True)
        self.wall[0] += 2 * 86400
        scheduler.tick()
        self.assertFalse((scheduler.directory / lost['segment']).exists())
        self.assertEqual(self.state(writer)['acked_seq'], lost['to_seq'])
        self.assertEqual(scheduler.snapshot()['segments_lost'], 1)
        following = scheduler.tick(force=True)
        with zipfile.ZipFile(scheduler.directory / following['segment']) as archive:
            losses = [json.loads(line) for line in archive.read('handoff_losses.jsonl').splitlines()]
        self.assertIn(('segment:' + lost['segment'], 'retention', lost['from_seq'] + 1, lost['to_seq']),
                      [(row['table_name'], row['reason'], row['from_seq'], row['to_seq']) for row in losses])

    def test_full_spool_pauses_production_and_size_cap_continues_next_tick(self):
        writer = self.writer()
        scheduler = self.scheduler(writer, max_bytes=1)
        writer.submit_raw(page(document((1, 2, 3))), ).result(15)
        first = scheduler.tick(force=True)
        self.assertFalse(first['complete'])
        self.assertIsNotNone(scheduler.tick())
        full = self.scheduler(self.writer_for_second_store(), quota_bytes=1)
        self.assertIsNone(full.tick(force=True))

    def writer_for_second_store(self):
        root = Path(self.directory.name) / 'second'
        root.mkdir()
        writer = HistoryWriter(root / 'history.db', raw_retention_days=1, raw_quota_bytes=64 * 1024**2)
        writer.start()
        self.addCleanup(writer.close)
        writer.submit_raw(page(document((1,)))).result(15)
        scheduler = handoff.HandoffScheduler(writer, self.spool, quota_bytes=64 * 1024**2, retention_ms=DAY,
                                             data_retention_ms=DAY, tick_s=3600, clock=self.clock,
                                             wall=lambda: self.wall[0]).start()
        scheduler.tick(force=True)
        scheduler.close(False)
        writer.submit_raw(page(document((2,), after=1), after=1)).result(15)
        return writer

    def test_existing_start_backfills_rows_from_before_handoff(self):
        store = HistoryStore(self.db).open()
        store.insert_raw_page(page(document((1, 2)), received=self.received()), 64 * 1024**2)
        store._insert_event(EventRecord.create(NODE_ID, 'core_state', self.received(), None, 'live', 'core_current'))
        store.close()
        writer = self.writer()
        scheduler = self.scheduler(writer, start='existing')
        built = scheduler.tick(force=True)
        self.assertFalse(scheduler.snapshot()['backfill_pending'])
        with zipfile.ZipFile(scheduler.directory / built['segment']) as archive:
            raw = [json.loads(line) for line in archive.read('raw_publications.jsonl').splitlines()]
            events = archive.read('events.jsonl').splitlines()
        self.assertEqual([row['publication_id'] for row in raw], [1, 2])
        self.assertEqual(len(events), 1)
        self.assertIsNone(self.state(writer)['backfill_bound_ms'])

    def test_clean_shutdown_publishes_pending_rows_and_restart_adopts_unrecorded_segment(self):
        writer = self.writer()
        scheduler = self.scheduler(writer)
        writer.submit_raw(page(document((1,)))).result(15)
        scheduler.close()
        self.assertEqual(len(handoff.segments(scheduler.directory)), 1)
        writer.submit_raw(page(document((2,), after=1), after=1)).result(15)
        with patch.object(handoff.HandoffScheduler, '_deliver', side_effect=RuntimeError('crash')):
            self.assertIsNone(scheduler.tick(force=True))
        self.assertEqual(scheduler.snapshot()['last_error'], 'RuntimeError')
        self.assertEqual(len(handoff.segments(scheduler.directory)), 2)
        scheduler.tick()
        self.assertEqual(scheduler.snapshot()['segments_adopted'], 1)
        self.assertEqual(self.state(writer)['delivered_seq'], handoff.segments(scheduler.directory)[-1][1])


class BackfillTests(unittest.TestCase):
    def test_batches_continue_where_the_previous_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / 'h.db').open()
            try:
                store.insert_raw_page(page(document(tuple(range(1, 8))), received=1000), 64 * 1024**2)
                store.enable_handoff('existing', 2000)
                done = [store.backfill_handoff(3) for _ in range(4)]
                self.assertEqual(done, [3, 3, 1, 0])
                seqs = [r[0] for r in store.connection.execute(
                    'SELECT handoff_seq FROM raw_publications ORDER BY rowid')]
                self.assertEqual(seqs, list(range(1, 8)))
                self.assertIsNone(store.handoff_state()['backfill_bound_ms'])
            finally:
                store.close()


class OptionTests(unittest.TestCase):
    def run_main(self, *extra):
        with patch('sys.stderr'), self.assertRaises(SystemExit) as caught:
            main(['--nodes', 'nodes.example.json'] + list(extra))
        return caught.exception.code

    def test_handoff_options_are_validated_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = ['--history-db', str(Path(directory) / 'h.db'), '--raw-capture', '--raw-retention-days', '1',
                   '--raw-quota-mib', '10']
            spool = ['--handoff-spool', directory, '--handoff-spool-quota-mib', '10']
            self.assertEqual(self.run_main(*spool, '--handoff-spool-retention-days', '1'), 2)
            self.assertEqual(self.run_main(*raw, *spool), 2)
            self.assertEqual(self.run_main(*raw, *spool, '--handoff-spool-retention-days', '2'), 2)
            self.assertEqual(self.run_main(*raw, '--handoff-spool', str(Path(directory) / 'missing'),
                                           '--handoff-spool-quota-mib', '10', '--handoff-spool-retention-days', '1'), 2)
            self.assertEqual(self.run_main('--handoff-start', 'existing'), 2)
            handoff.validate_spool(directory)


if __name__ == '__main__':
    unittest.main()
