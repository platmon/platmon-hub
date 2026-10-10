import contextlib
import http.client
import io
import json
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
        self.admin = RegistryAdmin(self.registry)
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
        values = {'Origin': origin, 'X-Platmon-Admin': '1', 'Content-Type': 'application/json'}
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

    def test_local_add_backup_uuid_and_reload_without_agent_requests(self):
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

    def test_csrf_and_forged_identity_never_write(self):
        original = self.registry.read_bytes()
        cases = [({'X-Platmon-Admin': None, 'X-Forwarded-User': 'admin'}, 403),
                 ({'X-Platmon-Admin': 'wrong'}, 403),
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
        for method in ('PUT', 'PATCH', 'DELETE', 'OPTIONS'):
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

    def test_admin_rate_budget_and_header_redaction(self):
        self.server.admin_budget = RequestBudget(rate=0, burst=1)
        with self.assertLogs('hub.access', 'INFO') as logs:
            self.assertEqual(self.request(headers={'Authorization': 'Bearer private-token'})[0], 201)
            self.assertEqual(self.request()[0], 429)
        self.assertNotIn('private-token', ''.join(logs.output))
        # List refreshes do not spend the mutation budget.
        self.assertEqual(self.request(method='GET')[0], 200)

    def test_fail_closed_configuration(self):
        with self.assertRaises(ValueError): HubServer(('0.0.0.0', 0), self.fleet, admin=self.admin)
        empty = Fleet([])
        try:
            with self.assertRaises(ValueError): HubServer(('127.0.0.1', 0), empty, admin=self.admin)
        finally: empty.close()
        for options in (['--bind', '0.0.0.0'], ['--no-registry-reload']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit), patch('hub.__main__.Fleet') as fleet:
                main(['--nodes', str(self.registry), '--web-admin'] + options)
            fleet.assert_not_called()

    def test_duplicate_sensitive_headers_and_global_budget_fail_closed(self):
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        for duplicate in ('Host', 'Origin', 'X-Platmon-Admin', 'Content-Length'):
            with self.subTest(header=duplicate):
                connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
                body = b'{"name":"Lab","host":"192.0.2.10"}'
                values = [('Host', origin[7:]), ('Origin', origin), ('X-Platmon-Admin', '1'),
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
                    headers={'Host': 'localhost', 'Origin': 'http://localhost', 'X-Platmon-Admin': '1', 'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 201)
                response.read()
            finally: connection.close()

    def test_management_list_requires_non_simple_header_and_local_origin(self):
        self.assertEqual(self.request()[0], 201)
        for headers, code in (({'X-Platmon-Admin': None}, 403), ({'Origin': 'http://evil.example'}, 403),
                              ({'Sec-Fetch-Site': 'cross-site'}, 403)):
            with self.subTest(headers=headers):
                result = self.request(method='GET', headers=headers)
                self.assertEqual(result[0], code)
                self.assertNotIn('192.0.2.10', json.dumps(result[2]))
        # Same-origin browser GET does not send Origin; the non-simple header is required.
        status, _, result = self.request(method='GET', headers={'Origin': None})
        self.assertEqual(status, 200)
        self.assertEqual(result['nodes'][0]['endpoint'], 'http://192.0.2.10:9797')
        self.assertEqual(set(result['nodes'][0]), {'node_id', 'name', 'endpoint', 'enabled'})

    def test_edit_disable_enable_remove_preserves_identity_and_history(self):
        identity = self.request()[2]['node_id']
        history = self.registry.with_name('history.db')
        history.write_bytes(b'preserved history')
        def loaded(): return self.request(method='GET')[2]['nodes'][0]
        node = loaded()
        changed = dict(name='Updated', host='192.0.2.11', port=12345, enabled=False, expected=node)
        status, _, result = self.request(changed, path='/api/admin/nodes/' + identity + '/update')
        self.assertEqual(status, 200)
        self.assertEqual(result['node_id'], identity)
        self.assertEqual(result['action'], 'update')
        self.assertEqual(load_registry(str(self.registry) + '.bak')[0].name, 'Lab')
        node = loaded()
        self.assertFalse(node['enabled'])
        self.assertEqual(node['endpoint'], 'http://192.0.2.11:12345')
        changed.update(enabled=True, expected=node)
        self.assertEqual(self.request(changed, path='/api/admin/nodes/' + identity + '/update')[0], 200)
        node = loaded()
        self.assertTrue(node['enabled'])
        self.assertEqual(self.request({'expected':node, 'confirm_node_id':identity}, path='/api/admin/nodes/' + identity + '/remove')[0], 200)
        self.assertEqual(load_registry(self.registry), ())
        self.assertEqual(history.read_bytes(), b'preserved history')
        self.fleet.watcher.check_once(); self.fleet.watcher._seen_at -= 1; self.fleet.watcher.check_once()
        self.assertEqual(self.fleet.list_nodes(), [])

    def test_stale_edit_and_remove_never_overwrite_newer_record(self):
        from hub.nodes_cli import update_registry
        identity = self.request()[2]['node_id']
        node = self.request(method='GET')[2]['nodes'][0]
        update_registry(self.registry, 'rename', node_id=identity, name='Changed by CLI')
        original = self.registry.read_bytes()
        update = dict(name='Stale browser', host='192.0.2.12', port=9797, enabled=True, expected=node)
        for path, body in (('/update', update), ('/remove', dict(expected=node, confirm_node_id=identity))):
            result = self.request(body, path='/api/admin/nodes/' + identity + path)
            self.assertEqual(result[0], 409)
            self.assertEqual(result[2]['error'], 'device_changed')
            self.assertEqual(self.registry.read_bytes(), original)

    def test_edit_and_remove_authorization_confirmation_and_validation(self):
        identity = self.request()[2]['node_id']
        node = self.request(method='GET')[2]['nodes'][0]
        original = self.registry.read_bytes()
        update = dict(name='Changed', host='192.0.2.12', port=9797, enabled=True, expected=node)
        for suffix, body in (('/update', update), ('/remove', dict(expected=node, confirm_node_id=identity))):
            for headers, code in (({'X-Platmon-Admin': None}, 403), ({'Origin': None}, 403), ({'Origin': 'http://evil.example'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403)):
                self.assertEqual(self.request(body, headers=headers, path='/api/admin/nodes/' + identity + suffix)[0], code)
        for body in ({'expected': node}, {'expected': node, 'confirm_node_id': 'wrong'},
                     {'expected':node, 'confirm_node_id':identity, 'delete_history':True}):
            self.assertEqual(self.request(body, path='/api/admin/nodes/' + identity + '/remove')[0], 400)
        broken = dict(update); broken['expected'] = dict(node, enabled='yes')
        self.assertEqual(self.request(broken, path='/api/admin/nodes/' + identity + '/update')[0], 400)
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertEqual(self.request(update, path='/api/admin/nodes/unknown/update')[0], 400)

    def test_edit_duplicate_endpoint_does_not_partially_rename(self):
        first = self.request()[2]['node_id']
        self.assertEqual(self.request({'name': 'Second', 'host': '192.0.2.11'})[0], 201)
        node = next(n for n in self.request(method='GET')[2]['nodes'] if n['node_id'] == first)
        original = self.registry.read_bytes()
        self.assertEqual(self.request(dict(name='Lost name', host='192.0.2.11', port=9797, enabled=False, expected=node),
            path='/api/admin/nodes/' + first + '/update')[0], 409)
        self.assertEqual(self.registry.read_bytes(), original)

    def test_concurrent_edit_has_one_winner_and_stale_retry_is_rejected(self):
        from concurrent.futures import ThreadPoolExecutor
        identity = self.request()[2]['node_id']
        node = self.request(method='GET')[2]['nodes'][0]
        path = '/api/admin/nodes/' + identity + '/update'
        def edit(name):
            return self.request(dict(name=name, host='192.0.2.10', port=9797, enabled=True, expected=node), path=path)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ('First edit', 'Second edit')))
        self.assertEqual(results.count(200), 1)
        self.assertIn(next(code for code in results if code != 200), (409, 503))
        self.assertEqual(edit('Stale retry'), 409)
        self.assertIn(load_registry(self.registry)[0].name, ('First edit', 'Second edit'))
