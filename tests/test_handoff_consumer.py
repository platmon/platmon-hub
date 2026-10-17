"""ADR-0004 stage 4: a reference consumer of the handoff contract, and handoff diagnostics.

The consumer here is deliberately minimal and uses only the published format: it is the
executable statement of the consumer obligations, not a post-processing system.
"""
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.client import HTTPConnection
from pathlib import Path

from hub import handoff
from hub.history.projector import EventRecord
from hub.history.store import read_connection
from hub.history.writer import HistoryWriter
from hub.http import HubServer
from hub.registry import Node
from hub.runtime import Fleet
from test_backend import NODE_ID
from test_history import wait_for
from test_replay import document, page

DAY = 86400000
KEYS = {'raw_publications.jsonl': ('node_id', 'instance_id', 'stream', 'publication_id'),
        'replay_losses.jsonl': ('node_id', 'instance_id', 'stream', 'from_id', 'reason'),
        'gaps.jsonl': ('gap_uid',), 'events.jsonl': ('event_id',), 'hub_sessions.jsonl': ('session_id',),
        'handoff_losses.jsonl': ('loss_id',)}


class ConsumerError(Exception):
    pass


class ReferenceConsumer:
    """Verify each segment and its chain, apply row versions by key, then acknowledge atomically."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.tables = {member: {} for member in KEYS}
        self.envelopes = {}
        self.last = None  # (to_seq, sha256)

    def poll(self):
        applied = []
        for path in sorted(self.directory.glob('*.zip')):
            first, last = (int(part) for part in path.stem.split('-'))
            if self.last is not None and last <= self.last[0]:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                if manifest['format'] != 'platmon-hub-handoff' or manifest['format_version'] != 1:
                    raise ConsumerError('unsupported format')
                expected_from = 0 if self.last is None else self.last[0]
                expected_previous = None if self.last is None else self.last[1]
                if (manifest['from_seq'], manifest['previous_sha256']) != (expected_from, expected_previous):
                    raise ConsumerError('broken chain at ' + path.name)
                members = {}
                for item in manifest['files']:
                    data = archive.read(item['name'])
                    if hashlib.sha256(data).hexdigest() != item['sha256']:
                        raise ConsumerError('member hash mismatch in ' + path.name)
                    members[item['name']] = [json.loads(line) for line in data.splitlines()]
            for row in members['raw_envelopes.jsonl']:
                self.envelopes[row['digest']] = row['envelope']
            for member, key in KEYS.items():
                for row in members[member]:
                    identity = tuple(row[name] for name in key)
                    current = self.tables[member].get(identity)
                    if current is None or current['handoff_seq'] < row['handoff_seq']:
                        self.tables[member][identity] = row
            self.last = (manifest['to_seq'], digest)
            self.acknowledge(path.name, digest)
            applied.append(path.name)
        return applied

    def acknowledge(self, segment, digest):
        acks = self.directory / 'acks'
        acks.mkdir(exist_ok=True)
        temporary = acks / ('.' + segment + '.tmp')
        temporary.write_text(digest, encoding='ascii')
        os.replace(temporary, acks / (segment + '.ack'))


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db, self.spool = root / 'history.db', root / 'spool'
        self.spool.mkdir()
        self.writer = HistoryWriter(self.db, raw_retention_days=1, raw_quota_bytes=64 * 1024**2)
        self.writer.start()
        self.addCleanup(self.writer.close)

    def scheduler(self, **kwargs):
        options = dict(quota_bytes=64 * 1024**2, retention_ms=DAY, data_retention_ms=DAY, tick_s=3600)
        options.update(kwargs)
        scheduler = handoff.HandoffScheduler(self.writer, self.spool, **options).start()
        self.addCleanup(scheduler.close, False)
        return scheduler

    def committed(self, label):
        reader = read_connection(self.db)
        try:
            return reader.execute('SELECT count(*) FROM events WHERE current_state=?', (label,)).fetchone()[0] == 1
        finally:
            reader.close()

    def event(self, label):
        self.writer.submit(EventRecord.create(NODE_ID, 'core_state', int(time.time() * 1000), None, label, 'core_current'))

    def test_consumer_rebuilds_committed_rows_and_its_acknowledgements_are_accepted(self):
        scheduler = self.scheduler()
        consumer = ReferenceConsumer(scheduler.directory)
        after = 0
        for round_number in range(3):
            ids = tuple(range(after + 1, after + 4))
            self.writer.submit_raw(page(document(ids, after=after), after=after)).result(15)
            after = ids[-1]
            self.event('round-%d' % round_number)
            wait_for(lambda: self.committed('round-%d' % round_number))
            scheduler.tick(force=True)
            self.assertEqual(len(consumer.poll()), 1)
            scheduler.tick()
        state = self.writer.handoff('handoff_state').result(15)
        self.assertEqual(state['acked_seq'], state['delivered_seq'])
        reader = read_connection(self.db)
        try:
            stored = {tuple(row) for row in reader.execute(
                'SELECT node_id, instance_id, stream, publication_id FROM raw_publications')}
            events = {row[0] for row in reader.execute('SELECT event_id FROM events')}
        finally:
            reader.close()
        self.assertEqual(set(consumer.tables['raw_publications.jsonl']), stored)
        self.assertEqual({key[0] for key in consumer.tables['events.jsonl']}, events)
        for row in consumer.tables['raw_publications.jsonl'].values():
            self.assertIn(row['envelope_digest'], consumer.envelopes)
            self.assertEqual(row['data']['sample']['sequence'], row['publication_id'])
        snapshot = scheduler.snapshot()
        self.assertEqual((snapshot['delivered_seq'], snapshot['acked_seq']), (state['delivered_seq'], state['acked_seq']))
        self.assertEqual(snapshot['unacknowledged_segments'], 0)

    def test_consumer_refuses_a_tampered_segment_and_hub_keeps_it_unacknowledged(self):
        scheduler = self.scheduler()
        self.writer.submit_raw(page(document((1, 2)))).result(15)
        built = scheduler.tick(force=True)
        path = scheduler.directory / built['segment']
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members['events.jsonl'] = b'{"tampered":true}\n'
        replacement = path.with_name('.rewrite.zip')
        with zipfile.ZipFile(replacement, 'w') as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        os.replace(replacement, path)
        with self.assertRaisesRegex(ConsumerError, 'hash mismatch'):
            ReferenceConsumer(scheduler.directory).poll()
        scheduler.tick()
        self.assertEqual(self.writer.handoff('handoff_state').result(15)['acked_seq'], 0)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db, self.spool = root / 'history.db', root / 'spool'
        self.spool.mkdir()

    def get(self, server, path):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_status_route_reports_positions_losses_and_warnings(self):
        writer = HistoryWriter(self.db, raw_retention_days=1, raw_quota_bytes=1024**2)
        fleet = Fleet([Node(NODE_ID, 'n', 'http://192.0.2.1:9797', False)], history=writer)
        server = HubServer(('127.0.0.1', 0), fleet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fleet.start()
            self.assertEqual(self.get(server, '/api/handoff/status'), (200, {'schema_version': 1, 'handoff': {'enabled': False}}))
            scheduler = handoff.HandoffScheduler(writer, self.spool, quota_bytes=1, retention_ms=DAY,
                                                 data_retention_ms=DAY, tick_s=3600).start()
            server.handoff = scheduler
            writer.submit_raw(page(document((1, 2)))).result(15)
            scheduler.tick(force=True)
            acks = scheduler.directory / 'acks'
            acks.mkdir()
            (acks / (scheduler.snapshot()['last_segment'] + '.ack')).write_text('0' * 64)
            scheduler.tick()
            status, body = self.get(server, '/api/handoff/status')
            handoff_status = body['handoff']
            self.assertEqual(status, 200)
            self.assertTrue(handoff_status['enabled'])
            self.assertEqual(handoff_status['delivered_seq'], handoff_status['next_seq'])
            self.assertEqual(handoff_status['acked_seq'], 0)
            self.assertGreater(handoff_status['unacknowledged_raw_bytes'], 0)
            self.assertIsNotNone(handoff_status['oldest_unacknowledged_received_ms'])
            self.assertIn(dict(table='hub_sessions', reason='not_handed_off', records=1, rows=1), handoff_status['losses'])
            self.assertEqual(handoff_status['warnings'][:2], ['spool_full', 'rejected_acknowledgement'])
            scheduler.close(False)
        finally:
            server.shutdown()
            server.server_close()
            fleet.close()


if __name__ == '__main__':
    unittest.main()
