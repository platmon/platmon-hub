"""ADR-0007: per-node raw capture level and sampled capture from live polls."""
import json
import unittest.mock
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from hub import protocol
from hub.admin import RegistryAdmin
from hub.history import codec
from hub.history.raw import sampled_core, sampled_observations
from hub.history.store import HistoryStore
from hub.history.writer import HistoryWriter
from hub.nodes_cli import main as nodes_main, update_registry
from hub.registry import Node, load_registry, node_record, validate_registry
from hub.replay import ReplayCollector
from hub.runtime import Fleet
from test_backend import INSTANCE, NODE_ID, st, stats
from test_history import wait_for
from test_replay import document, page

TIMING = dict(started_at=1700000000.5, completed_at=1700000000.6)


def timed_stats(seq):
    value = stats(seq)
    value['sample'].update(TIMING)
    return value


def observations(observation_id):
    group = dict(state='ok', stale_after_ms=15000, interval_ms=5000, collecting_for_ms=None,
                 observation=dict(TIMING, id=observation_id, duration_ms=1, age_ms=10, data_age_ms=10, stale=False),
                 collector=dict(state='ok', reason=None), data={'signal_dbm': -50})
    return {'schema_version': 2, 'instance_id': INSTANCE, 'groups': {'wifi': group}}


class State:
    def __init__(self, capture):
        self.node = Node(NODE_ID, 'node', 'http://192.0.2.1:9797', True, capture)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'nodes.json'
        self.record = dict(node_id=NODE_ID, name='node', endpoint='http://192.0.2.1:9797')

    def test_capture_field_is_optional_validated_and_default_omitted(self):
        self.assertEqual(validate_registry({'nodes': [self.record]})[0].capture, 'auto')
        for level in ('auto', 'replay', 'sampled', 'off'):
            node = validate_registry({'nodes': [dict(self.record, capture=level)]})[0]
            self.assertEqual(node.capture, level)
            self.assertEqual('capture' in node_record(node), level != 'auto')
        for bad in ('full', None, True, ''):
            with self.assertRaises(ValueError): validate_registry({'nodes': [dict(self.record, capture=bad)]})

    def test_cli_sets_capture_and_other_edits_keep_it(self):
        self.path.write_text(json.dumps({'nodes': [self.record]}), encoding='utf-8')
        with unittest.mock.patch('sys.stdout'):
            nodes_main(['--file', str(self.path), 'set-capture', NODE_ID, 'sampled'])
        update_registry(self.path, 'rename', node_id=NODE_ID, name='renamed')
        self.assertEqual(load_registry(self.path)[0].capture, 'sampled')
        self.assertEqual(json.loads(self.path.read_text())['nodes'][0]['capture'], 'sampled')
        with unittest.mock.patch('sys.stdout'):
            nodes_main(['--file', str(self.path), 'set-capture', NODE_ID, 'auto'])
        self.assertNotIn('capture', json.loads(self.path.read_text())['nodes'][0])

    def test_web_edit_accepts_loaded_record_with_capture_and_keeps_it(self):
        self.path.write_text(json.dumps({'nodes': [dict(self.record, capture='off')]}), encoding='utf-8')
        admin = RegistryAdmin(self.path)
        loaded = admin.list_nodes()['nodes'][0]
        self.assertEqual(loaded['capture'], 'off')
        admin.update(NODE_ID, dict(name='edited', host='192.0.2.1', port=9797, enabled=True, expected=loaded))
        node = load_registry(self.path)[0]
        self.assertEqual((node.name, node.capture), ('edited', 'off'))


class BuilderAndStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = HistoryStore(Path(self.directory.name) / 'raw.db').open()
        self.addCleanup(self.store.close)
        self.c = self.store.connection

    def test_builders_mark_live_poll_and_skip_unidentifiable_documents(self):
        record = sampled_core(NODE_ID, timed_stats(7), 1000, 5000)
        envelope = json.loads(record.envelope_json)
        self.assertEqual(envelope, dict(source='live_poll', path='/api/stats', capture='sampled',
                                        gap_before_observed=False, poll_interval_ms=5000))
        self.assertEqual(record.rows[0][:2], ('core', 7))
        self.assertIsNone(sampled_core(NODE_ID, stats(7), 1000, 5000))
        slow = sampled_observations(NODE_ID, observations(3), ('wifi', 'battery'), 1000, 10000)
        self.assertEqual([row[:2] for row in slow.rows], [('wifi', 3)])
        self.assertIsNone(sampled_observations(NODE_ID, observations(3), ('battery',), 1000, 10000))

    def test_sampled_rows_share_replay_identity_without_cursor_or_losses(self):
        record = sampled_core(NODE_ID, timed_stats(1), 1000, 5000)
        result = self.store.insert_sampled(record, 64 * 1024**2)
        self.assertEqual((result['inserted'], result['duplicates']), (1, 0))
        self.assertEqual(self.store.insert_sampled(record, 64 * 1024**2)['duplicates'], 1)
        self.assertIsNone(self.store.replay_cursor(NODE_ID, 'core'))
        self.assertEqual(self.c.execute('SELECT count(*) FROM replay_losses').fetchone()[0], 0)
        # A later replay page of the same publication keeps the first (sampled) copy.
        self.assertEqual(self.store.insert_raw_page(page(document((1, 2))), 64 * 1024**2)['inserted'], 1)
        rows = self.c.execute('SELECT publication_id,gap_before,e.envelope_json FROM raw_publications p '
                              'JOIN raw_envelopes e USING (envelope_id) ORDER BY publication_id').fetchall()
        self.assertEqual(json.loads(rows[0][2])['source'], 'live_poll')
        self.assertEqual(rows[0][1], 0)
        self.assertNotIn('source', json.loads(rows[1][2]))
        usage = self.c.execute('SELECT encoded_bytes FROM raw_usage').fetchone()[0]
        charged = sum(self.c.execute('SELECT coalesce(sum(encoded_bytes),0) FROM ' + t).fetchone()[0]
                      for t in ('raw_publications', 'raw_envelopes', 'raw_dictionaries'))
        self.assertEqual(usage, charged)


class CollectorTests(unittest.TestCase):
    def test_levels_choose_replay_or_sampled_per_stream(self):
        streams = ('core', 'wifi')
        collector = ReplayCollector(writer=None)
        for level, expected in (('sampled', streams), ('off', ()), ('replay', ())):
            token = State(level)
            self.assertEqual(collector.sampled_streams(NODE_ID, token, streams), expected)
        token = State('auto')
        with collector._lock:
            collector._node(NODE_ID, token)
        self.assertEqual(collector.sampled_streams(NODE_ID, token, streams), ())
        collector.fail(NODE_ID, token, 'core', protocol.OptionalUnsupported('404'))
        self.assertEqual(collector.sampled_streams(NODE_ID, token, streams), ('core',))
        snapshot = collector.snapshot()
        self.assertEqual(snapshot['capture'], {NODE_ID: 'auto'})
        self.assertEqual(snapshot['nodes'][NODE_ID]['core']['source'], 'sampled')
        self.assertEqual(snapshot['nodes'][NODE_ID]['wifi']['source'], 'replay')

    def test_sampled_and_off_nodes_never_request_replay(self):
        for level in ('sampled', 'off'):
            self.assertIsNone(ReplayCollector(writer=None).ready(NODE_ID, State(level)))


class FleetTests(unittest.TestCase):
    """An Agent without replay (404, like platmon 1.2.x) delivers sampled rows under auto."""

    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'raw.db'
        self.requested, self.sequence, owner = [], [0], self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                path = self.path.split('?')[0]
                owner.requested.append(path)
                code, payload = 200, None
                if path == '/api/status':
                    payload = st()
                elif path == '/api/stats':
                    owner.sequence[0] += 1
                    payload = timed_stats(owner.sequence[0])
                elif path == '/api/observations':
                    payload = observations(owner.sequence[0])
                else:
                    code, payload = 404, {'code': 'replay_unavailable'}
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.endpoint = 'http://127.0.0.1:' + str(self.server.server_port)

    def run_fleet(self, capture, predicate):
        writer = HistoryWriter(self.path, raw_retention_days=1, raw_quota_bytes=64 * 1024**2)
        fleet = Fleet([Node(NODE_ID, 'node', self.endpoint, True, capture)], history=writer,
                      core_interval=0.1, observations_interval=0.1)
        fleet.start()
        try:
            wait_for(lambda: predicate(writer.snapshot()['raw']))
        finally:
            fleet.close()
            writer.close()
        c = sqlite3.connect(self.path)
        try:
            return c.execute('SELECT stream, e.envelope_json FROM raw_publications JOIN raw_envelopes e '
                             'USING (envelope_id)').fetchall()
        finally:
            c.close()

    def test_auto_falls_back_to_sampled_for_streams_without_replay(self):
        rows = self.run_fleet('auto', lambda raw: raw['sampled_rows'] >= 3)
        self.assertIn('/api/samples', self.requested)
        self.assertTrue(rows)
        self.assertTrue(all(json.loads(envelope)['source'] == 'live_poll' for _, envelope in rows))
        self.assertIn('core', {stream for stream, _ in rows})

    def test_off_stores_no_raw_rows_and_requests_no_replay(self):
        self.run_fleet('off', lambda raw: len([p for p in self.requested if p == '/api/stats']) >= 3)
        self.assertNotIn('/api/samples', self.requested)
        self.assertNotIn('/api/observation-samples', self.requested)
        c = sqlite3.connect(self.path)
        try:
            self.assertEqual(c.execute('SELECT count(*) FROM raw_publications').fetchone()[0], 0)
        finally:
            c.close()


if __name__ == '__main__':
    unittest.main()
