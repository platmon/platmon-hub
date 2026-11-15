"""Alert webhook: configuration, which transitions leave Hub, and delivery against a loopback server."""
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hub.alerts import AlertEngine, load_config, validate_rules
from hub.webhook import QUEUE_LIMIT, WebhookSender, target, validate_webhook
from test_alerts import MemoryHistory
from test_backend import NODE_ID


class Receiver:
    """A loopback endpoint answering with the queued status codes (200 once they run out)."""

    def __init__(self, statuses=(), delay=0):
        self.bodies, self.headers, self.statuses, self.delay = [], [], list(statuses), delay
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                time.sleep(owner.delay)
                owner.bodies.append(json.loads(body))
                owner.headers.append(dict(self.headers))
                status = owner.statuses.pop(0) if owner.statuses else 200
                self.send_response(status)
                if status in (301, 302):
                    self.send_header('Location', 'http://127.0.0.1:9/elsewhere')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/hook/secret-token?key=abc' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.01)


class ConfigTests(unittest.TestCase):
    def test_webhook_is_optional_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'alerts.json'
            path.write_text(json.dumps({'rules': []}))
            self.assertEqual(load_config(path), ([], None))
            path.write_text(json.dumps({'rules': [], 'webhook': {'url': 'https://hooks.example.com/T0/B0/x'}}))
            self.assertEqual(load_config(path)[1], {'url': 'https://hooks.example.com/T0/B0/x', 'timeout_seconds': 5})
        self.assertEqual(validate_webhook({'url': 'http://[::1]:8080/a', 'timeout_seconds': 30})['timeout_seconds'], 30)
        for source in ({'url': 'ftp://example.com/'}, {'url': 'https://user:pw@example.com/'},
                       {'url': 'https://token@example.com/'}, {'url': 'https:///path'}, {'url': 'https://exa mple.com/'},
                       {'url': 'https://example.com:99999/'}, {'url': 'https://example.com/' + 'a' * 2048},
                       {'url': 'https://example.com/', 'timeout_seconds': 0}, {'url': 'https://example.com/', 'timeout_seconds': 31},
                       {'url': 'https://example.com/', 'headers': {}}, {'url': 7}, {}, []):
            with self.assertRaises(ValueError, msg=source):
                validate_webhook(source)
        with self.assertRaises(ValueError):
            validate_rules({'rules': [], 'extra': 1})
        self.assertEqual(target('https://hooks.example.com:8443/T0/secret?x=1'), 'https://hooks.example.com:8443')


class FakeSender:
    def __init__(self):
        self.payloads = []

    def submit(self, payload):
        self.payloads.append(payload)

    def snapshot(self):
        return {'delivered': 0}


class TransitionTests(unittest.TestCase):
    def test_only_firing_escalation_and_resolution_leave_hub(self):
        now = [0]
        rules = validate_rules({'rules': [dict(id='cpu', condition='cpu_percent', warning=80, critical=95,
                                               duration_seconds=10, recovery_seconds=5, cooldown_seconds=0)]})
        sender = FakeSender()
        engine = AlertEngine(rules, MemoryHistory(), clock=lambda: now[0], wall=lambda: 1000 + now[0], webhook=sender)
        node = dict(node_id=NODE_ID, name='Bench board', enabled=True, headline='live', data_current=True)

        def tick(seconds, value):
            now[0] = seconds
            node['overview'] = {'cpu_percent': value}
            engine.evaluate([node])
            return [p['event'] for p in sender.payloads]

        self.assertEqual(tick(0, 90), [])                       # Pending stays in Hub
        self.assertEqual(tick(5, 70), [])                       # back to Normal: never fired
        tick(6, 90)
        self.assertEqual(tick(16, 90), ['firing'])
        first = sender.payloads[0]
        self.assertEqual((first['node_name'], first['rule_id'], first['severity'], first['value'], first['state'],
                          first['previous_state']), ('Bench board', 'cpu', 'warning', 90, 'Active', 'Pending'))
        self.assertEqual(first['text'], 'platmon-hub: cpu firing on Bench board (warning, value 90)')
        self.assertEqual(tick(17, 99), ['firing', 'escalated'])
        self.assertEqual(tick(18, 70), ['firing', 'escalated'])  # Recovering stays in Hub
        self.assertEqual(tick(19, 90), ['firing', 'escalated'])  # rebound to Active: it never resolved
        tick(20, 70)
        self.assertEqual(tick(25, 70), ['firing', 'escalated', 'resolved'])
        self.assertEqual(sender.payloads[-1]['reason'], 'condition')
        tick(26, 90)
        tick(36, 90)
        self.assertEqual(engine.snapshot()['webhook'], {'delivered': 0})
        engine.evaluate([])                                     # the node was removed while Active
        self.assertEqual((sender.payloads[-1]['event'], sender.payloads[-1]['reason']), ('resolved', 'node_inactive'))
        self.assertEqual(sender.payloads[-1]['node_name'], 'Bench board')

    def test_no_webhook_reports_null(self):
        engine = AlertEngine([], MemoryHistory())
        self.assertIsNone(engine.snapshot()['webhook'])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        log = logging.getLogger('hub.webhook')
        log.disabled = True  # failures are expected here; the counters are what is checked
        self.addCleanup(setattr, log, 'disabled', False)

    def sender(self, receiver, **kwargs):
        sender = WebhookSender({'url': receiver.url, 'timeout_seconds': 1}, retry_delays=(0, 0), **kwargs)
        sender.start()
        self.addCleanup(sender.close)
        return sender

    def test_delivers_json_and_never_shows_the_path(self):
        receiver = Receiver()
        self.addCleanup(receiver.close)
        sender = self.sender(receiver)
        sender.submit({'event': 'firing', 'text': 'x'})
        wait_for(lambda: sender.snapshot()['delivered'] == 1)
        self.assertEqual(receiver.bodies, [{'event': 'firing', 'text': 'x'}])
        self.assertEqual(receiver.headers[0]['Content-Type'], 'application/json')
        self.assertTrue(receiver.headers[0]['User-Agent'].startswith('platmon-hub/'))
        snapshot = sender.snapshot()
        self.assertNotIn('secret-token', json.dumps(snapshot))
        self.assertEqual((snapshot['failed'], snapshot['last_error'], snapshot['queued']), (0, None, 0))

    def test_retries_then_counts_a_failure(self):
        receiver = Receiver(statuses=[500, 503])
        self.addCleanup(receiver.close)
        sender = self.sender(receiver)
        sender.submit({'n': 1})                                  # third attempt succeeds
        wait_for(lambda: sender.snapshot()['delivered'] == 1)
        receiver.statuses = [500, 500, 500]
        sender.submit({'n': 2})
        wait_for(lambda: sender.snapshot()['failed'] == 1)
        self.assertEqual(sender.snapshot()['last_error'], 'http_500')
        self.assertEqual(len(receiver.bodies), 6)
        receiver.statuses = [302, 302, 302]                      # redirects are not followed
        sender.submit({'n': 3})
        wait_for(lambda: sender.snapshot()['failed'] == 2)
        self.assertEqual(sender.snapshot()['last_error'], 'http_302')

    def test_unreachable_and_slow_endpoints_fail_without_blocking_submit(self):
        receiver = Receiver(delay=1.5)
        self.addCleanup(receiver.close)
        slow = WebhookSender({'url': receiver.url, 'timeout_seconds': 1}, retry_delays=())
        slow.start()
        self.addCleanup(slow.close)
        started = time.monotonic()
        for n in range(QUEUE_LIMIT + 5):
            slow.submit({'n': n})
        self.assertLess(time.monotonic() - started, 0.5)
        wait_for(lambda: slow.snapshot()['failed'] >= 1)
        self.assertEqual(slow.snapshot()['last_error'], 'timeout')
        self.assertGreaterEqual(slow.snapshot()['dropped'], 4)  # the queue keeps the newest 256
        # A peer that drops the connection without an answer (a closed port is not used: Windows retries
        # the connect until the timeout, so it would read as 'timeout' there).
        listener = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(listener.close)

        def hang_up():
            connection, _ = listener.accept()
            connection.close()
        threading.Thread(target=hang_up, daemon=True).start()
        closed = WebhookSender({'url': 'http://127.0.0.1:%d/hook' % listener.getsockname()[1], 'timeout_seconds': 1},
                               retry_delays=())
        closed.start()
        self.addCleanup(closed.close)
        closed.submit({'n': 0})
        wait_for(lambda: closed.snapshot()['failed'] == 1)
        self.assertEqual(closed.snapshot()['last_error'], 'connection')


if __name__ == '__main__':
    unittest.main()
