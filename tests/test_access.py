import http.client
import threading
import unittest
from hub.access import RequestBudget
from hub.http import HubServer
from hub.runtime import Fleet


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.fleet = Fleet([])
        self.server = HubServer(('127.0.0.1', 0), self.fleet)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.fleet.close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method='GET', path='/api/nodes', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: connection.close()

    def test_write_methods_never_grant_admin_even_with_spoofed_identity(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            self.assertEqual(self.request(method, '/api/nodes', {'Authorization':'Bearer fake', 'X-Forwarded-User':'admin'})[0], 405)

    def test_host_bounds_headers_and_redaction(self):
        self.assertEqual(self.request(headers={'Host':'evil.example'})[0], 403)
        self.assertEqual(self.request(path='http://evil.example/api/nodes')[0], 403)
        self.assertEqual(self.request(path='/'+'x'*4096)[0], 431)
        self.assertEqual(self.request(headers={'X-Large':'x'*17000})[0], 431)
        with self.assertLogs('hub.access', 'INFO') as output:
            status, headers, body = self.request(path='/api/events?token=secret-value', headers={'Authorization':'Bearer secret-value'})
        self.assertNotIn('secret-value', ''.join(output.output))
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_rate_budget_and_bounded_admission(self):
        now = [0]
        budget = RequestBudget(rate=1, burst=2, clock=lambda:now[0])
        self.assertTrue(budget.take()); self.assertTrue(budget.take()); self.assertFalse(budget.take())
        now[0] = 1
        self.assertTrue(budget.take()); self.assertFalse(budget.take())
        self.server.request_budget = RequestBudget(rate=0, burst=0)
        self.assertEqual(self.request()[0], 429)
        from test_history import wait_for
        wait_for(lambda: self.server._requests._value == 32)
        for _ in range(32): self.assertTrue(self.server._requests.acquire(False))
        try: self.assertEqual(self.request()[0], 503)
        finally:
            for _ in range(32): self.server._requests.release()
