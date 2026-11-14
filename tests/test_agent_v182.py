"""Synthetic platmon 1.4.0–1.8.2 additions over the pinned 1.2.0 fixture, never real device traffic.

Fields follow docs/reference/api.md, battery.md and processes.md at upstream tag v1.8.2
(949c9581a3ccff3688a85cd1dcf1ed93261a8beb). 1.4.0–1.7.0 changed no answer Hub reads.
"""
import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hub import protocol
from hub.history.projector import OBSERVATION_GROUPS
from hub.history.raw import STREAMS
from hub.registry import Node
from hub.state import NodeState
from test_agent_v130 import LEVELS
from test_backend import FakeClock, NODE_ID


def fixture():
    document = json.loads((Path(__file__).parent / 'fixtures' / 'agent-v1.2.0.json').read_text(encoding='utf-8'))
    document['status']['levels'] = copy.deepcopy(LEVELS)
    battery = document['observations']['groups']['battery']['data']['batteries'][0]
    battery['temp_c'] = 31.5
    group = copy.deepcopy(document['observations']['groups']['battery'])
    group.update(interval_ms=5000, stale_after_ms=15000, observation=dict(group['observation'], id=3))
    group['data'] = {
        'scope': 'host', 'total': 512, 'running': 1, 'blocked': 0, 'zombie': 0, 'threads': 2036, 'top': 10,
        'processes': [{'pid': 6696, 'name': 'gnome-shell', 'kernel': False, 'state': 'S', 'cpu_percent': 5.2,
                       'rss_bytes': 317444096, 'threads': 26, 'unit': 'org.gnome.Shell@ubuntu.service'}]}
    document['observations']['groups']['processes'] = group
    return document


class Agent182Tests(unittest.TestCase):
    def setUp(self):
        self.document = fixture()
        self.state = NodeState(Node(NODE_ID, 'Synthetic 1.8.2 host', 'http://192.0.2.1:9797'), clock=FakeClock())

    def accept(self):
        accepted = self.state.accept_core(self.document['status'], self.document['stats'], received_at_ms=1000)
        self.assertTrue(accepted.record.valid())
        self.assertTrue(self.state.accept_observations(self.document['observations']))
        return self.state.snapshot()

    def test_live_schemas_are_unchanged(self):
        self.assertEqual(self.document['status']['schema_version'], 1)
        self.assertEqual(self.document['stats']['schema_version'], 1)
        self.assertEqual(self.document['observations']['schema_version'], 2)

    def test_battery_temperature_is_kept_for_the_detail(self):
        snapshot = self.accept()
        battery = snapshot['observations']['battery']['data']['batteries'][0]
        self.assertEqual(battery['temp_c'], 31.5)
        self.assertEqual(snapshot['battery_percent'], 89)

    def test_null_battery_temperature_is_accepted(self):
        self.document['observations']['groups']['battery']['data']['batteries'][0]['temp_c'] = None
        snapshot = self.accept()
        self.assertIsNone(snapshot['observations']['battery']['data']['batteries'][0]['temp_c'])

    def test_processes_group_is_accepted_but_not_stored(self):
        snapshot = self.accept()
        self.assertEqual(snapshot['headline'], 'live')
        self.assertIsNone(snapshot['optional_error'])
        self.assertFalse(snapshot['optional_alert_error'])
        # History and raw capture read only the groups they know; processes is not one of them.
        self.assertNotIn('processes', OBSERVATION_GROUPS)
        self.assertNotIn('processes', STREAMS)


class HostRefusedTests(unittest.TestCase):
    """platmon 1.8.0+ answers 421 on every path for a Host name it does not accept."""

    def setUp(self):
        class Misdirected(BaseHTTPRequestHandler):
            def do_GET(self):
                # platmon-server 1.8.2 answers with an HTML error page and the refusal in the reason phrase.
                body = b'<!DOCTYPE HTML><title>Error response</title>'
                self.send_response(421, "Host 'orin.lab.example' is not this device's address or name; "
                                        "to accept it, list it in [http] hosts")
                self.send_header('Content-Type', 'text/html;charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, format, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Misdirected)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.endpoint = 'http://127.0.0.1:' + str(server.server_address[1])

    def test_421_is_a_host_refusal_on_every_endpoint(self):
        client = protocol.HttpAgentClient()
        for path in ('/api/status', '/api/stats', '/api/observations'):
            with self.subTest(path=path), self.assertRaises(protocol.HostRefused) as caught:
                client.get(self.endpoint, path, 2)
            self.assertEqual(caught.exception.category, 'error')
        with self.assertRaises(protocol.HostRefused):
            client.get_replay(self.endpoint, 'core', '8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91', 0, 2)

    def test_node_names_the_refusal_and_stays_an_error(self):
        client = protocol.HttpAgentClient()
        state = NodeState(Node(NODE_ID, 'Renamed host', self.endpoint), clock=FakeClock())
        for path, fail in (('/api/status', state.fail_core), ('/api/observations', state.fail_observations)):
            try:
                client.get(self.endpoint, path, 2)
            except protocol.AgentFailure as exc:
                fail(exc)
        snapshot = state.snapshot()
        self.assertEqual(snapshot['headline'], 'error')
        self.assertEqual(snapshot['core_error'], 'host_refused')
        self.assertEqual(snapshot['optional_error'], 'host_refused')
        self.assertEqual(snapshot['consecutive_transport_failures'], 0)


if __name__ == '__main__':
    unittest.main()
