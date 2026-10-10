import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hub.__main__ import main
from hub.access import RequestBudget
from hub.admin import RegistryAdmin
from hub.filelock import FileLock
from hub.http import HubServer
from hub.registry import load_registry
from hub.runtime import Fleet


class WebRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.registry = root / 'nodes.json'
        self.registry.write_text('{"nodes": []}', encoding='utf-8')
        self.token = root / 'admin-token.txt'
        self.token.write_text('a' * 64 + '\n', encoding='utf-8')
        self.token.chmod(0o600)
        self.admin = RegistryAdmin(self.registry, self.token)
        self.fleet = Fleet([], registry_path=self.registry)
        self.addCleanup(self.fleet.close)
        self.server = HubServer(('127.0.0.1', 0), self.fleet, admin=self.admin)
        self.server.admin_budget = RequestBudget(rate=100, burst=200)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, data=None, headers=None, method='POST', path='/api/admin/nodes', raw=None):
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        values = {'Origin': origin, 'Authorization': 'Bearer ' + 'a' * 64, 'Content-Type': 'application/json'}
        if headers:
            for key, value in headers.items():
                if value is None: values.pop(key, None)
                else: values[key] = value
        body = raw if raw is not None else json.dumps(data or {'name': 'Lab', 'host': '192.0.2.10'})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body if method != 'GET' else None, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def test_authenticated_add_backup_uuid_and_reload_without_agent_requests(self):
        original = self.registry.read_bytes()
        with patch('urllib.request.urlopen') as request:
            status, headers, body = self.request({'name': 'Lab', 'host': '::1', 'port': 9797, 'enabled': False})
        request.assert_not_called()
        self.assertEqual(status, 201)
        node = load_registry(self.registry)[0]
        self.assertEqual(node.node_id, body['node_id'])
        self.assertEqual(node.endpoint, 'http://[::1]:9797')
        self.assertFalse(node.enabled)
        self.assertEqual(Path(str(self.registry) + '.bak').read_bytes(), original)
        # Apply the ordinary watcher deterministically, without starting Agent polls.
        self.fleet.watcher.check_once()
        self.fleet.watcher._seen_at -= 1
        self.fleet.watcher.check_once()
        self.assertEqual(self.fleet.list_nodes()[0]['node_id'], node.node_id)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("form-action 'none'", headers['Content-Security-Policy'])

    def test_authentication_csrf_and_forged_identity_never_write(self):
        original = self.registry.read_bytes()
        cases = [({'Authorization': None, 'X-Forwarded-User': 'admin'}, 401),
                 ({'Authorization': 'Bearer fake'}, 401),
                 ({'Origin': None}, 403), ({'Origin': 'http://evil.example'}, 403),
                 ({'Origin': 'http://127.0.0.1:9798'}, 403),
                 ({'Host': 'evil.example'}, 403),
                 ({'Sec-Fetch-Site': 'cross-site'}, 403)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], expected)
                self.assertEqual(self.registry.read_bytes(), original)

    def test_disabled_and_other_write_routes_remain_405(self):
        self.server.admin = None
        self.assertFalse(self.request(method='GET', path='/api/admin/status')[2]['enabled'])
        self.assertEqual(self.request()[0], 405)
        self.server.admin = self.admin
        for method in ('PUT', 'PATCH', 'DELETE'):
            self.assertEqual(self.request(method=method)[0], 405)
        self.assertEqual(self.request(path='/api/nodes')[0], 405)
        self.assertEqual(self.request(path='/api/admin/nodes?host=evil')[0], 405)

    def test_invalid_and_oversized_input_preserves_registry(self):
        original = self.registry.read_bytes()
        for data in ({'name': '', 'host': '192.0.2.10'},
                     {'name': 'Lab', 'host': 'http://192.0.2.10'},
                     {'name': 'Lab', 'host': 'user:private-token@192.0.2.10'},
                     {'name': 'Lab', 'host': '192.0.2.10', 'port': True},
                     {'name': 'Lab', 'host': '192.0.2.10', 'port': 0},
                     {'name': 'Lab', 'host': '192.0.2.10', 'enabled': 'yes'},
                     {'name': 'Lab', 'host': '192.0.2.10', 'endpoint': 'http://192.0.2.10'}):
            with self.subTest(data=data):
                result = self.request(data)
                self.assertEqual(result[0], 400)
                self.assertNotIn('private-token', json.dumps(result))
        self.assertEqual(self.request(raw='{"name":"Lab","host":"192.0.2.10","host":"192.0.2.11"}')[0], 400)
        self.assertEqual(self.request(raw='{bad')[0], 400)
        self.assertEqual(self.request(raw='x' * 4097)[0], 413)
        self.assertEqual(self.request(headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request(headers={'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertEqual(self.registry.read_bytes(), original)

    def test_duplicate_and_busy_registry_are_recoverable(self):
        self.assertEqual(self.request()[0], 201)
        original = self.registry.read_bytes()
        self.assertEqual(self.request()[0], 409)
        with FileLock(str(self.registry) + '.lock'):
            self.assertEqual(self.request({'name': 'Other', 'host': '192.0.2.11'})[0], 503)
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertEqual(self.request({'name': 'Other', 'host': '192.0.2.11'})[0], 201)

    def test_admin_rate_budget_and_token_redaction(self):
        self.server.admin_budget = RequestBudget(rate=0, burst=1)
        with self.assertLogs('hub.access', 'INFO') as logs:
            self.assertEqual(self.request(headers={'Authorization': 'Bearer private-token'})[0], 401)
            self.assertEqual(self.request()[0], 429)
        self.assertNotIn('private-token', ''.join(logs.output))
        self.assertNotIn('a' * 64, ''.join(logs.output))

    def test_token_permissions_and_fail_closed_configuration(self):
        for text in ('short', 'A' * 64, 'g' * 64, 'a' * 200):
            self.token.write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError): RegistryAdmin(self.registry, self.token)
        if os.name != 'nt':
            self.token.write_text('a' * 64, encoding='utf-8')
            self.token.chmod(0o644)
            with self.assertRaises(ValueError): RegistryAdmin(self.registry, self.token)
        with self.assertRaises(ValueError): HubServer(('0.0.0.0', 0), self.fleet, admin=self.admin)
        empty = Fleet([])
        try:
            with self.assertRaises(ValueError): HubServer(('127.0.0.1', 0), empty, admin=self.admin)
        finally: empty.close()
        for options in (['--bind', '0.0.0.0'], ['--no-registry-reload']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit), patch('hub.__main__.Fleet') as fleet:
                main(['--nodes', str(self.registry), '--admin-token-file', str(self.token)] + options)
            fleet.assert_not_called()

    def test_duplicate_sensitive_headers_and_global_budget_fail_closed(self):
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        for duplicate in ('Host', 'Origin', 'Authorization', 'Content-Length'):
            with self.subTest(header=duplicate):
                connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
                body = b'{"name":"Lab","host":"192.0.2.10"}'
                values = [('Host', origin[7:]), ('Origin', origin), ('Authorization', 'Bearer ' + 'a' * 64),
                          ('Content-Length', str(len(body))), ('Content-Type', 'application/json')]
                try:
                    connection.putrequest('POST', '/api/admin/nodes', skip_host=True)
                    for key, value in values:
                        connection.putheader(key, value)
                        if key == duplicate: connection.putheader(key, value)
                    connection.endheaders(body)
                    response = connection.getresponse()
                    self.assertIn(response.status, (400, 401, 403))
                    response.read()
                finally: connection.close()
        self.assertEqual(load_registry(self.registry), ())
        self.server.request_budget = RequestBudget(rate=0, burst=0)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(load_registry(self.registry), ())

    def test_standard_http_port_origin_uses_bare_host(self):
        # Exercise browser serialization for port 80 without binding a privileged port.
        actual_port = self.server.server_port
        with patch.object(self.server, 'server_port', 80):
            connection = http.client.HTTPConnection('127.0.0.1', actual_port, timeout=2)
            try:
                connection.request('POST', '/api/admin/nodes', body=json.dumps({'name': 'Lab', 'host': '192.0.2.10'}),
                    headers={'Host': 'localhost', 'Origin': 'http://localhost', 'Authorization': 'Bearer ' + 'a' * 64, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 201)
                response.read()
            finally: connection.close()
