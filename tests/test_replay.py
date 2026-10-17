"""Replay durability, malformed pages, resource bounds and live isolation."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from hub import protocol
from hub.__main__ import main
from hub.export import ExportError
from hub.history.raw import validate_page, STREAMS
from hub.history.schema import VERSION
from hub.history.store import HistoryStore
from hub.history.writer import HistoryWriter
from hub.raw_export import export_raw
from hub.registry import Node
from hub.replay import fetch_page, ReplayCollector
from hub.runtime import Fleet
from test_backend import NODE_ID, INSTANCE, st, stats
from test_history import wait_for


def document(ids=(1, 2), *, stream='core', instance=INSTANCE, latest=None, after=0):
    rows = []
    for pid in ids:
        timing = dict(started_at=1700000000.0, completed_at=1699999999.0,
                      duration_ms=1, age_ms=60000, data_age_ms=60001, stale=True)
        if stream == 'core':
            data = stats(pid, instance, 60001, 5000)
            data['sample'].update(timing)
            data['power'] = {'rail': 0, 'missing': None}
            data['sensor_meta'] = {'/power/rail': {'provider': 'fixture', 'unit': 'W'}}
        else:
            data = dict(state='stale', stale_after_ms=5000, interval_ms=1000,
                        observation=dict(timing, id=pid), collector=dict(state='error', reason='read_failure'),
                        data=None, collecting_for_ms=None)
        rows.append(dict(id=pid, gap_before=pid == 2, data=data))
    latest = max(ids, default=after) if latest is None else latest
    expected, losses = after+1, []
    for pid in ids:
        if pid > expected:
            losses.append({'from': expected, 'to': pid-1, 'reason': 'buffer_unavailable'})
        expected = pid+1
    if expected <= latest:
        losses.append({'from': expected, 'to': latest, 'reason': 'buffer_unavailable'})
    return dict(schema_version=1, data_schema_version=1 if stream == 'core' else 2,
                instance_id=instance, stream=stream, clock={'source': 'fixture'}, historical=True,
                retention_s=600, max_bytes=16777216, max_records=4096,
                max_record_bytes=1048576, max_response_bytes=2097152,
                oldest_id=min(ids) if ids else None, latest_id=latest,
                next_after=latest, has_more=False, missing_ranges=losses, records=rows)


def page(doc=None, *, after=0, received=None, previous=None, previous_after=0):
    doc = document() if doc is None else doc
    return validate_page(doc, NODE_ID, doc['instance_id'], doc['stream'], after,
                         int(time.time()*1000) if received is None else received, previous, previous_after)


class ValidationTests(unittest.TestCase):
    def test_pinned_agent_publisher_fixture(self):
        fixture = json.loads((Path(__file__).parent/'fixtures'/'replay-schema1.json').read_text())
        for name in ('core', 'battery'):
            value = page(fixture[name])
            self.assertEqual(value.stream, name)
            self.assertEqual(value.next_after, 1)
            payload = json.loads(value.records[0][1])
            self.assertTrue(payload['sample' if name == 'core' else 'observation']['stale'])
            if name == 'battery':
                self.assertEqual(payload['data']['current_ua'], -123)
                self.assertIsNone(payload['data']['voltage_uv'])

    def test_stale_full_data_gaps_zero_null_clock_reversal_and_ownership(self):
        doc = document((2, 4), latest=5)
        frozen = page(doc)
        doc['records'][0]['data']['power']['rail'] = 999
        payload = json.loads(frozen.records[0][1])
        self.assertEqual(payload['power'], {'rail': 0, 'missing': None})
        self.assertEqual([r[:2] for r in frozen.losses], [(1, 1), (3, 3), (5, 5)])
        self.assertTrue(payload['sample']['stale'])
        self.assertLess(payload['sample']['completed_at'], payload['sample']['started_at'])
        self.assertTrue(frozen.records[0][2])

    def test_empty_expired_and_independent_groups(self):
        self.assertEqual(page(document(())).next_after, 0)
        self.assertEqual(page(document((), latest=9)).losses, ((1, 9, 'buffer_unavailable'),))
        for stream in STREAMS[1:]:
            value = page(document((1,), stream=stream))
            self.assertIsNone(json.loads(value.records[0][1])['data'])
            self.assertEqual(value.stream, stream)

    def test_invalid_pages_rejected_without_partial_salvage(self):
        mutations = [
            lambda d: d.update(schema_version=2),
            lambda d: d.update(data_schema_version=4),
            lambda d: d.update(historical=False),
            lambda d: d.update(next_after=3),
            lambda d: d.update(has_more=1),
            lambda d: d.update(oldest_id=True),
            lambda d: d.update(extra_metadata='x'*16385),
            lambda d: d.update(records=list(reversed(d['records']))),
            lambda d: d['records'][1].update(id=1),
            lambda d: d['records'][1]['data']['sample'].update(sequence=3),
            lambda d: d['records'][1]['data']['sample'].update(instance_id='other'),
            lambda d: d['records'][1]['data']['sample'].update(started_at=float('nan')),
            lambda d: d['records'][1]['data']['sample'].update(stale=False),
            lambda d: d['records'][1].update(gap_before=0),
            lambda d: d['records'][1]['data'].update(extra=float('inf')),
            lambda d: d['records'][1]['data'].update(extra='x'*1048576),
            lambda d: d.update(missing_ranges=[{'from': 1, 'to': 2, 'reason': 'buffer_unavailable'}]),
            lambda d: d['records'].pop(0),
        ]
        for mutate in mutations:
            doc = document()
            mutate(doc)
            with self.subTest(mutation=mutate), self.assertRaises(protocol.AgentFailure):
                page(doc)

    def test_pagination_has_more_and_coverage(self):
        doc = document()
        doc.update(latest_id=4, has_more=True)
        self.assertEqual(page(doc).next_after, 2)
        doc['records'] = []
        with self.assertRaises(protocol.ProtocolFailure): page(doc)


class RawStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'raw.db'
        self.store = HistoryStore(self.path).open()
        self.addCleanup(self.store.close)

    def insert(self, value):
        return self.store.insert_raw_page(value, 64*1024**2)

    def test_commit_retry_first_payload_and_restart_recovery(self):
        first = page()
        self.insert(first)
        doc = document()
        doc['records'][0]['data']['power']['rail'] = 99
        self.assertEqual(self.insert(page(doc))['duplicates'], 2)
        row = self.store.connection.execute('SELECT data_json,received_at_ms FROM raw_publications WHERE publication_id=1').fetchone()
        self.assertEqual(json.loads(row[0])['power']['rail'], 0)
        self.assertEqual(row[1], first.received_at_ms)
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.replay_cursor(NODE_ID, 'core'), (INSTANCE, 2))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM core_samples').fetchone()[0], 0)

    def test_atomic_losses_and_restart_boundary(self):
        self.insert(page(document((2,), latest=4)))
        replacement = page(document((1,), instance='new'), previous=INSTANCE, previous_after=4)
        self.insert(replacement)
        self.insert(replacement)  # acknowledgement lost after commit
        self.assertEqual(self.store.replay_cursor(NODE_ID, 'core'), ('new', 1))
        losses = self.store.connection.execute('SELECT instance_id,from_id,to_id,reason FROM replay_losses ORDER BY received_at_ms,from_id').fetchall()
        self.assertIn((INSTANCE, 5, None, 'agent_restart_unknown_extent'), losses)
        self.assertEqual(len(losses), 3)
        with self.assertRaises(ValueError): self.insert(page(document((3,), after=2), after=2))
        self.assertEqual(self.store.replay_cursor(NODE_ID, 'core'), ('new', 1))

    def test_quota_and_injected_failure_roll_back_everything(self):
        value = page(document((2,)))
        with self.assertRaisesRegex(ValueError, 'raw_quota'):
            self.store.insert_raw_page(value, 1)
        self.assertIsNone(self.store.replay_cursor(NODE_ID, 'core'))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM replay_losses').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT encoded_bytes FROM raw_usage').fetchone()[0], 0)
        self.store.connection.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON replay_cursors BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.Error): self.insert(value)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM raw_publications').fetchone()[0], 0)
        self.assertIsNone(self.store.replay_cursor(NODE_ID, 'core'))

    def test_retention_preserves_cursors_and_loss_evidence(self):
        self.insert(page(document((2,)), received=1000))
        self.assertEqual(self.store.prune_raw(2000), 1)
        self.assertEqual(self.store.replay_cursor(NODE_ID, 'core'), (INSTANCE, 2))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM replay_losses').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT encoded_bytes FROM raw_usage').fetchone()[0], 0)

    def test_v5_migration_backup_and_rollback(self):
        self.store.close()
        old = self.path.with_name('old.db')
        # Existing migrations produce precisely the old schema without modifying a fixture database.
        with patch('hub.history.store.MIGRATION_6', 'INVALID SQL;'):
            with self.assertRaises(sqlite3.Error): HistoryStore(old).open()
        c = sqlite3.connect(old)
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 5)
        self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='raw_publications'").fetchone()[0], 0)
        c.close()
        backups = list(old.parent.glob('old.db.v5-backup-*'))
        self.assertEqual(len(backups), 1)
        c = sqlite3.connect(backups[0])
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 5)
        c.close()
        migrated = HistoryStore(old).open()
        self.assertEqual(migrated.connection.execute('PRAGMA user_version').fetchone()[0], VERSION)
        migrated.close()

    def test_crash_after_commit_and_before_commit(self):
        self.store.close()
        script = '''
import os, sys
sys.path.insert(0, 'tests')
from test_replay import page, document
from hub.history.store import HistoryStore
s = HistoryStore(sys.argv[1]).open()
s.insert_raw_page(page(), 10000000)
s.connection.execute('BEGIN IMMEDIATE')
s.connection.execute('UPDATE replay_cursors SET after_id=999')
os._exit(0)
'''
        subprocess.run([sys.executable, '-c', script, str(self.path)], check=True, timeout=10)
        self.store.open()
        self.assertEqual(self.store.replay_cursor(NODE_ID, 'core'), (INSTANCE, 2))

    def test_bounded_consistent_raw_export_preserves_metadata(self):
        self.insert(page(document((2,)), received=1000))
        output = self.path.with_suffix('.zip')
        manifest = export_raw(self.path, output, 0, 2000, [NODE_ID])
        with zipfile.ZipFile(output) as z:
            row = json.loads(z.read('raw_publications.jsonl'))
            self.assertTrue(row['gap_before'])
            self.assertEqual(row['reported_started_at'], 1700000000.0)
            self.assertEqual(row['received_at_ms'], 1000)
            self.assertEqual(row['data']['power'], {'rail': 0, 'missing': None})
            self.assertEqual(row['envelope']['clock']['source'], 'fixture')
            self.assertEqual(json.loads(z.read('replay_losses.jsonl'))['from_id'], 1)
        self.assertEqual(manifest['format_version'], 1)
        with self.assertRaises(FileExistsError): export_raw(self.path, output, 0, 2000)
        failed = output.with_name('failed.zip')
        for budgets in ({'max_rows': 1}, {'max_bytes': 100}):
            with self.assertRaises(ExportError): export_raw(self.path, failed, 0, 2000, **budgets)
            self.assertFalse(failed.exists())
        self.assertFalse(list(output.parent.glob('.platmon-raw-*')))

    def test_raw_export_deadline_cleans_temporary_file(self):
        self.insert(page(document((2,)), received=1000))
        output = self.path.with_suffix('.zip')
        with patch('hub.raw_export.time.perf_counter', side_effect=[100, 101]):
            with self.assertRaisesRegex(ExportError, 'deadline exceeded'):
                export_raw(self.path, output, 0, 2000, timeout=1)
        self.assertFalse(output.exists())
        self.assertFalse(list(output.parent.glob('.platmon-raw-*')))


class WriterAndSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'raw.db'

    def writer(self, **kwargs):
        return HistoryWriter(self.path, raw_retention_days=1, raw_quota_bytes=1024**2, **kwargs)

    def test_ack_is_commit_not_admission_and_cursor_reads_use_writer(self):
        writer = self.writer()
        future = writer.submit_raw(page())
        self.assertFalse(future.done())
        writer.start()
        try:
            self.assertEqual(future.result(3)['after_id'], 2)
            self.assertEqual(writer.replay_cursor(NODE_ID, 'core').result(3), (INSTANCE, 2))
        finally:
            self.assertTrue(writer.close()['flushed'])
        self.assertEqual(writer.snapshot()['raw']['queued_bytes'], 0)

    def test_bounded_queue_failure_and_disabled_capture(self):
        writer = self.writer()
        futures = [writer.submit_raw(page()) for _ in range(20)]
        self.assertEqual(sum(f.done() for f in futures), 4)
        self.assertLessEqual(writer.snapshot()['raw']['queued_bytes'], writer.raw_queue_bytes)
        writer.start()
        writer.close()
        disabled = HistoryWriter(self.path)
        self.assertIsInstance(disabled.submit_raw(page()).exception(), RuntimeError)
        disabled.close()

    def test_commit_failure_returns_failure_without_advancing(self):
        class Broken(HistoryStore):
            def insert_raw_page(self, *args): raise sqlite3.OperationalError('full')
        writer = self.writer(store_factory=Broken)
        writer.start()
        try:
            with self.assertRaises(sqlite3.Error): writer.submit_raw(page()).result(3)
            self.assertIsNone(writer.replay_cursor(NODE_ID, 'core').result(3))
        finally:
            writer.close()

    def test_lost_acknowledgement_does_not_stop_writer_or_duplicate_rows(self):
        writer = self.writer()
        acknowledgement = writer.submit_raw(page())
        self.assertTrue(acknowledgement.cancel())
        writer.start()
        try:
            self.assertEqual(writer.replay_cursor(NODE_ID, 'core').result(3), (INSTANCE, 2))
            self.assertEqual(writer.submit_raw(page()).result(3)['duplicates'], 2)
            self.assertEqual(writer.snapshot()['raw']['persisted'], 2)
        finally:
            self.assertTrue(writer.close()['flushed'])

    def test_live_state_unchanged_by_stale_replay_and_registry_race(self):
        class Client:
            def get(self, endpoint, path, deadline): return st() if path == '/api/status' else stats()
            def get_replay(self, endpoint, stream, instance, after, deadline): return document(stream=stream)
        writer = self.writer()
        writer.start()
        fleet = Fleet([Node(NODE_ID, 'node', 'http://127.0.0.1:9797')], history=writer, client=Client())
        try:
            fleet.core_once(NODE_ID)
            before = fleet.get_node(NODE_ID)
            token = fleet.nodes[NODE_ID]
            fleet.replay._node(NODE_ID, token)
            fleet.replay_once(NODE_ID, token, 'core', None)
            wait_for(lambda: writer.snapshot()['raw']['committed_pages'] == 1)
            self.assertEqual(fleet.get_node(NODE_ID)['stats']['sample']['sequence'], before['stats']['sample']['sequence'])
            self.assertEqual(fleet.get_node(NODE_ID)['headline'], 'live')
            class Race(Client):
                def get_replay(self, *args):
                    fleet.apply_registry([])
                    return document()
            fleet.client = Race()
            fleet.replay_once(NODE_ID, token, 'core', (INSTANCE, 2))
            self.assertEqual(writer.snapshot()['raw']['committed_pages'], 1)
            self.assertEqual(fleet.replay.snapshot()['nodes'], {})
        finally:
            fleet.close()

    def test_round_robin_backlog_and_commit_wait(self):
        writer = self.writer()
        writer.start()
        collector = ReplayCollector(writer)
        token = object()
        seen = []
        try:
            for _ in STREAMS:
                job = None
                deadline = time.monotonic()+3
                while job is None and time.monotonic() < deadline:
                    job = collector.ready(NODE_ID, token)
                    threading.Event().wait(.01)
                self.assertIsNotNone(job)
                name, cursor = job
                seen.append(name)
                doc = document(stream=name)
                collector.accept(NODE_ID, token, name, page(doc), True)
            self.assertEqual(seen, list(STREAMS))
        finally:
            writer.close()

    def test_conflicts_unsupported_and_schema_failures_visible(self):
        collector = ReplayCollector(self.writer())
        token = object()
        collector._node(NODE_ID, token)
        cases = [('core', protocol.ReplayConflict('cursor_ahead', INSTANCE), 'stopped'),
                 ('storage', protocol.ContractFailure('schema'), 'stopped'),
                 ('wifi', protocol.OptionalUnsupported('404'), 'unavailable'),
                 ('battery', protocol.ReplayConflict('instance_changed', 'new'), 'retrying'),
                 ('probe', protocol.TransportFailure('timeout'), 'retrying')]
        for stream, exc, expected in cases:
            collector.fail(NODE_ID, token, stream, exc)
            self.assertEqual(collector.snapshot()['nodes'][NODE_ID][stream]['state'], expected)

    def test_cli_requires_explicit_raw_capacity(self):
        for extra in (['--raw-capture'], ['--raw-capture', '--history-db', str(self.path)], ['--raw-retention-days', '1']):
            with patch('sys.stderr'), self.assertRaises(SystemExit) as caught:
                main(['--nodes', 'nodes.example.json']+extra)
            self.assertEqual(caught.exception.code, 2)


class ReplayHttpTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlsplit, parse_qs
        self.reply = document()
        self.code = 200
        self.requested = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                path = urlsplit(self.path)
                owner.requested.append((path.path, parse_qs(path.query)))
                payload = owner.reply
                if path.path == '/api/status':
                    payload = st()
                elif path.path == '/api/stats':
                    payload = stats()
                elif path.path == '/api/observations':
                    payload = {'schema_version': 2, 'instance_id': INSTANCE, 'groups': {}}
                body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_response(owner.code if 'samples' in path.path else 200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(close)
        self.endpoint = 'http://127.0.0.1:'+str(self.server.server_port)
        self.client = protocol.HttpAgentClient()

    def test_encoded_allowlist_live_and_replay_byte_limits(self):
        special = 'id?group=evil&next=/api/control'
        self.client.get_replay(self.endpoint, 'battery', special, 0, 1)
        path, params = self.requested[-1]
        self.assertEqual(path, '/api/observation-samples')
        self.assertEqual(params['instance_id'], [special])
        self.assertEqual(params['group'], ['battery'])
        for stream in ('../control', 'core?url=http://evil', '', None):
            with self.assertRaises(ValueError): self.client.get_replay(self.endpoint, stream, INSTANCE, 0, 1)
        with self.assertRaises(ValueError): self.client.get(self.endpoint, '/api/samples?after=0', 1)
        self.reply = {'data': 'x'*(600*1024)}
        self.assertIn('data', self.client.get_replay(self.endpoint, 'core', INSTANCE, 0, 1))
        # The ordinary reader retains its 512 KiB bound.
        with self.assertRaises(protocol.ProtocolFailure): self.client._get(self.endpoint, '/api/samples', 1, self.client.max_bytes)
        self.reply = {'data': 'x'*(2*1024**2)}
        with self.assertRaises(protocol.ProtocolFailure): self.client.get_replay(self.endpoint, 'core', INSTANCE, 0, 1)

    def test_conflict_unavailable_invalid_json_and_redirect(self):
        for code in ('instance_changed', 'cursor_ahead'):
            self.code, self.reply = 409, dict(code=code, instance_id='new')
            with self.assertRaises(protocol.ReplayConflict) as caught:
                self.client.get_replay(self.endpoint, 'core', INSTANCE, 0, 1)
            self.assertEqual(caught.exception.code, code)
        self.code, self.reply = 404, dict(code='replay_unavailable')
        with self.assertRaises(protocol.OptionalUnsupported): self.client.get_replay(self.endpoint, 'wifi', INSTANCE, 0, 1)
        self.code = 200
        for invalid in (b'{"x":NaN}', b'{"x":1,"x":2}', b'not-json'):
            self.reply = invalid
            with self.assertRaises(protocol.ProtocolFailure): self.client.get_replay(self.endpoint, 'core', INSTANCE, 0, 1)
        self.code, self.reply = 302, {}
        with self.assertRaises(protocol.ProtocolFailure): self.client.get_replay(self.endpoint, 'core', INSTANCE, 0, 1)

    def test_real_transport_persistence_export_and_status(self):
        from http.client import HTTPConnection
        from hub.http import HubServer
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory)/'history.db'
            writer = HistoryWriter(database, raw_retention_days=1, raw_quota_bytes=16*1024**2)
            fleet = Fleet([Node(NODE_ID, 'fixture', self.endpoint)], history=writer)
            server = HubServer(('127.0.0.1', 0), fleet)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                fleet.start()
                wait_for(lambda: writer.snapshot()['raw']['committed_pages'] >= 1)
                wait_for(lambda: fleet.get_node(NODE_ID)['headline'] == 'live')
                wait_for(lambda: fleet.replay.snapshot()['nodes'][NODE_ID]['core']['after_id'] == 2)
                c = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                c.request('GET', '/api/replay/status')
                response = c.getresponse()
                status = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(status['replay']['nodes'][NODE_ID]['core']['after_id'], 2)
                c.close()
                output = Path(directory)/'raw.zip'
                end = int(time.time()*1000)+1000
                export_raw(database, output, end-60000, end)
                with zipfile.ZipFile(output) as z:
                    self.assertEqual(len(z.read('raw_publications.jsonl').splitlines()), 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                fleet.close()
