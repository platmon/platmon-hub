"""Loopback-default read-only Hub JSON endpoints."""

import json
import logging
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from .access import RequestBudget
from .admin import AdminError, unique_object
from .history.query import QueryError, parameters, query_history
from .history.events import query_events


ASSET_ROOT = Path(__file__).resolve().parent / "web"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("assets/brand/favicon.svg", "image/svg+xml"),
    "/assets/brand/platmon-icon-light.svg": ("assets/brand/platmon-icon-light.svg", "image/svg+xml"),
    "/assets/brand/platmon-icon-dark.svg": ("assets/brand/platmon-icon-dark.svg", "image/svg+xml"),
    "/assets/brand/platmon-logo-light.svg": ("assets/brand/platmon-logo-light.svg", "image/svg+xml"),
    "/assets/brand/platmon-logo-dark.svg": ("assets/brand/platmon-logo-dark.svg", "image/svg+xml"),
}


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, fleet, admin=None):
        if admin is not None and (address[0] != '127.0.0.1' or fleet.watcher is None or
                fleet.watcher.path.resolve() != admin.registry_path):
            raise ValueError('web registry administration requires loopback and the same active registry watcher')
        self.admin = admin
        self.admin_budget = RequestBudget(rate=0.2, burst=5)
        self.fleet = fleet
        self.request_budget = RequestBudget()
        self._requests = threading.BoundedSemaphore(32)
        super().__init__(address, HubHandler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def process_request(self, request, address):
        if not self._requests.acquire(blocking=False):
            try:
                # Read the bounded GET headers before closing: unread inbound bytes
                # can turn a delivered 503 into a TCP reset on Windows.
                deadline = time.monotonic()+0.1
                header = bytearray()
                while b'\r\n\r\n' not in header and len(header) < 16384:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0: break
                    request.settimeout(remaining)
                    chunk = request.recv(min(4096, 16384-len(header)))
                    if not chunk: break
                    header.extend(chunk)
                request.settimeout(0.1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._requests.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._requests.release()


class HubHandler(BaseHTTPRequestHandler):
    server_version = 'platmon-hub'
    sys_version = ''

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Dashboard selection changes abort in-flight requests. The peer has
            # gone away; finish this request and release its admission slot.
            self.close_connection = True

    def log_request(self, code='-', size='-'):
        # Never log query strings, credentials, arbitrary paths or headers.
        logging.getLogger('hub.access').info('request method=%s status=%s',
            self.command if self.command in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS') else 'other', code)

    def log_message(self, format, *args):
        logging.getLogger('hub.access').warning('HTTP protocol error or timeout')

    def end_headers(self):
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        super().end_headers()

    def _request_path(self):
        if len(self.path) > 4096 or sum(len(k)+len(v) for k,v in self.headers.items()) > 16384:
            self._reply(431, {'error': 'request_too_large'})
            return None
        try:
            host = urlsplit('http://' + self.headers.get('Host', '')).hostname
            path = urlsplit(self.path)
        except ValueError:
            self._reply(400, {'error': 'invalid_request'})
            return None
        # A loopback listener must not answer a browser rebinding an arbitrary domain.
        if self.server.server_address[0] in ('127.0.0.1', '::1') and host not in ('127.0.0.1', 'localhost', '::1'):
            self._reply(403, {'error': 'untrusted_host'})
            return None
        if path.scheme or path.netloc:
            self._reply(400, {'error': 'invalid_request'})
            return None
        if not self.server.request_budget.take():
            self._reply(429, {'error': 'rate_limited'})
            return None
        return path

    def do_GET(self):
        path = self._request_path()
        if path is None:
            return
        if path.path == '/api/events' and not path.fragment:
            self._events(path.query)
        elif path.path.startswith('/api/nodes/') and path.path.endswith('/history') and path.path.count('/') == 4 and not path.fragment:
            self._history(path)
        elif path.query or path.fragment:
            self._reply(404, {"error": "not_found"})
        elif path.path in ASSETS:
            self._static(*ASSETS[path.path])
        elif path.path == "/healthz":
            self._reply(200, {"status": "ok"})
        elif path.path == "/api/registry/status":
            watcher = self.server.fleet.watcher
            self._reply(200, {"schema_version": 1, "registry": watcher.snapshot() if watcher else {"enabled": False}})
        elif path.path == "/api/history/status":
            history = self.server.fleet.history
            self._reply(200, {"schema_version": 1, "history": history.snapshot() if history else {"enabled": False, "state": "disabled"}})
        elif path.path == "/api/admin/status":
            self._reply(200, {"schema_version": 1, "enabled": self.server.admin is not None, "local_port": self.server.server_port})
        elif path.path == "/api/alerts":
            alerts = self.server.fleet.alerts
            self._reply(200, dict(schema_version=1, **(alerts.snapshot() if alerts else {"enabled": False, "alerts": []})))
        elif path.path == "/api/nodes":
            self._reply(200, {"schema_version": 1, "nodes": self.server.fleet.list_nodes()})
        elif path.path.startswith("/api/nodes/") and path.path.count("/") == 3:
            node = self.server.fleet.get_node(path.path.split("/")[-1])
            self._reply(200, {"schema_version": 1, "node": node}) if node is not None else self._reply(404, {"error": "not_found"})
        else:
            self._reply(404, {"error": "not_found"})

    def _events(self, query):
        try:
            history = self.server.fleet.history
            if history is None: raise QueryError('history_disabled', 503)
            if history.snapshot()['schema_version'] is None: raise QueryError('events_unavailable', 503)
            self._reply(200, query_events(history.path, query))
        except QueryError as exc:
            self._reply(exc.status, {'schema_version': 1, 'error': exc.code})

    def _history(self, path):
        fleet = self.server.fleet
        node_id = path.path.split('/')[3]
        if fleet.get_node(node_id) is None:
            self._reply(404, {'error': 'not_found'})
            return
        try:
            args = parameters(path.query)
            if fleet.history is None:
                raise QueryError('history_disabled', 503)
            if fleet.history.snapshot()['schema_version'] is None:
                raise QueryError('history_unavailable', 503)
            result = query_history(fleet.history.path, node_id, *args)
            self._reply(200, result)
        except QueryError as exc:
            self._reply(exc.status, {'schema_version': 1, 'error': exc.code})

    def do_POST(self):
        if self.server.admin is None or self.path != '/api/admin/nodes':
            self._read_only()
            return
        if self._request_path() is None:
            return
        # Strict local Origin/Host matching protects against CSRF and rebinding.
        host = self.headers.get('Host', '').lower()
        allowed = ('127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port))
        if (len(self.headers.get_all('Host', [])) != 1 or host not in allowed or
                len(self.headers.get_all('Origin', [])) != 1 or self.headers.get('Origin') != 'http://' + host or
                self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin'):
            self._reply(403, {'error': 'untrusted_origin', 'message': 'Use the local Hub page to add devices.'})
            return
        if not self.server.admin_budget.take():
            self._reply(429, {'error': 'rate_limited', 'message': 'Wait a few seconds before retrying.'})
            return
        if (len(self.headers.get_all('Authorization', [])) != 1 or
                not self.server.admin.authorized(self.headers.get('Authorization'))):
            self._reply(401, {'error': 'unauthorized', 'message': 'Enter the correct administrator token.'})
            return
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            self._reply(400, {'error': 'invalid_body', 'message': 'A bounded JSON body is required.'})
            return
        try:
            length = int(self.headers.get('Content-Length'))
        except ValueError:
            length = -1
        if not 1 <= length <= 4096:
            self._reply(413, {'error': 'invalid_body', 'message': 'Device registration body must be at most 4 KiB.'})
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self._reply(415, {'error': 'invalid_body', 'message': 'JSON content is required.'})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete body')
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            self._reply(400, {'error': 'invalid_body', 'message': 'Invalid JSON body.'})
            return
        try:
            self._reply(201, self.server.admin.add(data))
        except AdminError as exc:
            self._reply(exc.status, {'schema_version': 1, 'error': exc.code, 'message': exc.message})

    def _read_only(self):
        self._reply(405, {"error": "read_only"})

    do_PUT = do_PATCH = do_DELETE = _read_only

    def _static(self, filename, media_type):
        # Exact route-to-file mapping: no user-selected paths or directory traversal.
        data = (ASSET_ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _reply(self, status, body):
        output = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(output)))
        self.end_headers()
        self.wfile.write(output)
