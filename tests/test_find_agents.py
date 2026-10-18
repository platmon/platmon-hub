"""Operator Agent finder against fake loopback Agents; no real network is probed."""
import importlib.util
import io
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hub.registry import load_registry

path = Path(__file__).resolve().parents[1]/'scripts'/'find-agents.py'
spec = importlib.util.spec_from_file_location('find_agents_test', path)
finder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finder)

STATUS = dict(schema_version=1, instance_id='11111111-1111-4111-8111-111111111111', state='ready', ready=True)
STATS = dict(platform='Jetson Orin', model='NVIDIA Jetson Orin Nano Developer Kit',
             system=dict(os='Ubuntu 22.04.5 LTS', arch='aarch64', hostname='ubuntu'))


def serve(routes):
    """A loopback server answering path -> (status, content type, body); others 404."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, kind, body = routes.get(self.path, (404, 'application/json', '{}'))
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def agent_routes(footer=True, replay=True, observations=True, status=STATUS):
    routes = {'/api/status': (200, 'application/json', status), '/api/stats': (200, 'application/json', STATS)}
    if footer:
        routes['/'] = (200, 'text/html; charset=utf-8', '<main></main>\n<footer>platmon v1.3.0</footer>')
    if replay:
        routes['/api/samples?limit=1'] = (200, 'application/json', dict(records=[]))
    if observations:
        routes['/api/observations'] = (200, 'application/json', {})
    return routes


def closed_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class FindAgentsTests(unittest.TestCase):
    def start(self, routes):
        server = serve(routes)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def directory(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def survey(self, ports, nodes=()):
        return finder.survey(['127.0.0.1'], ports, list(nodes), 0.5, 2, 8, local={'127.0.0.1'})

    def test_identifies_new_agent_with_exact_version_and_replay(self):
        port = self.start(agent_routes())
        [agent] = self.survey([port])
        self.assertEqual(agent['endpoint'], f'http://127.0.0.1:{port}')
        self.assertEqual((agent['registry'], agent['state'], agent['version'], agent['version_exact'], agent['replay']),
                         ('new', 'ready', '1.3.0', True, True))
        self.assertEqual((agent['hostname'], agent['platform'], agent['arch']), ('ubuntu', 'Jetson Orin', 'aarch64'))
        self.assertTrue(agent['this_host'])

    def test_infers_minimum_version_without_web_page(self):
        port = self.start(agent_routes(footer=False, replay=False))
        [agent] = self.survey([port])
        self.assertEqual((agent['version'], agent['version_exact'], agent['replay']), ('1.2.0+', False, False))
        legacy = self.start(agent_routes(footer=False, replay=False, observations=False,
                                         status=dict(ready=True, state='ready')))
        [old] = self.survey([legacy])
        self.assertEqual(old['version'], 'pre-1.0')
        self.assertFalse(old['supported'])

    def test_other_services_are_not_agents(self):
        port = self.start({'/api/status': (200, 'application/json', dict(hello='world'))})
        [other] = self.survey([port])
        self.assertEqual((other['state'], other['registry']), ('not platmon', '-'))

    def test_marks_registered_and_checks_registered_endpoints_outside_targets(self):
        port, dead = self.start(agent_routes()), closed_port()
        registry = self.directory()/'nodes.json'
        registry.write_text(json.dumps({'nodes': [
            dict(node_id='22222222-2222-4222-8222-222222222222', name='orin', endpoint=f'http://127.0.0.1:{port}'),
            dict(node_id='33333333-3333-4333-8333-333333333333', name='gone', endpoint=f'http://127.0.0.1:{dead}', enabled=False)]}))
        agents = self.survey([port], load_registry(registry))
        by_name = {a['name']: a for a in agents}
        self.assertEqual(by_name['orin']['registry'], 'registered')
        self.assertEqual((by_name['gone']['registry'], by_name['gone']['state']), ('registered, disabled', 'no answer'))
        self.assertIn('registered as orin', finder.table(agents))

    def test_this_hosts_address_matches_a_loopback_registration(self):
        self.assertEqual(finder.comparable('http://192.0.2.5:9797', {'192.0.2.5'}),
                         finder.comparable('http://127.0.0.1:9797', {'192.0.2.5'}))

    def test_add_asks_per_agent_and_writes_through_the_registry_update(self):
        first, second = self.start(agent_routes()), self.start(agent_routes())
        agents = self.survey([first, second])
        registry = self.directory()/'nodes.json'
        answers = iter(['', 'skip'])
        added = finder.add_agents(agents, registry, False, ask=lambda label, default: next(answers) or default,
                                  out=io.StringIO())
        # The image-default hostname gives way to the platform and the last address part.
        self.assertEqual([name for name, _, _ in added], ['jetson-orin-1'])
        [node] = load_registry(registry)
        self.assertEqual(node.endpoint, agents[0]['endpoint'])

    def test_add_yes_skips_unsupported_agents(self):
        agents = [dict(endpoint='http://192.0.2.9:9797', registry='new', supported=False, version='pre-1.0')]
        registry = self.directory()/'nodes.json'
        out = io.StringIO()
        self.assertEqual(finder.add_agents(agents, registry, True, out=out), [])
        self.assertIn('older than Hub supports', out.getvalue())
        self.assertFalse(registry.exists())

    def test_suggested_names_avoid_image_defaults_and_duplicates(self):
        agent = dict(endpoint='http://192.168.55.53:9797', hostname='ubuntu', platform='Jetson Orin')
        self.assertEqual(finder.suggest_name(agent, set()), 'jetson-orin-53')
        self.assertEqual(finder.suggest_name(agent, {'jetson-orin-53'}), 'jetson-orin-53-2')
        self.assertEqual(finder.suggest_name(dict(agent, hostname='samchi-pi'), set()), 'samchi-pi')
        self.assertEqual(finder.suggest_name(dict(agent, hostname='samchi-pi'), {'samchi-pi'}), 'jetson-orin-53')

    def test_targets_are_bounded_and_private_by_default(self):
        hosts, _ = finder.parse_targets(['192.168.1.0/30', '192.168.1.1', '10.0.0.5-10.0.0.6'], 1024, False)
        self.assertEqual(hosts, ['192.168.1.1', '192.168.1.2', '10.0.0.5', '10.0.0.6'])
        self.assertEqual(finder.parse_targets(['fd00::1-fd00::2'], 8, False)[0], ['fd00::1', 'fd00::2'])
        for value, message in (('8.8.8.0/24', 'not a private network'), ('10.0.0.0/16', 'more than 1024'),
                               ('10.0.0.9-10.0.0.1', 'lower to a higher'), ('10.0.0.x', '10.0.0.x:')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                finder.parse_targets([value], 1024, False)
        self.assertEqual(len(finder.parse_targets(['8.8.8.8'], 1, True)[0]), 1)


if __name__ == '__main__':
    unittest.main()
