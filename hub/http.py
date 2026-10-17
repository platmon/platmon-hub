"""Loopback-default read-only Hub JSON endpoints."""

import json
import logging
import socket
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
    "/assets/data.js": ("data.js", "text/javascript; charset=utf-8"),
    "/assets/devices.js": ("devices.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("assets/brand/hub-favicon.svg", "image/svg+xml"),
    "/assets/brand/favicon.svg": ("assets/brand/favicon.svg", "image/svg+xml"),
    "/assets/brand/hub-icon-light.svg": ("assets/brand/hub-icon-light.svg", "image/svg+xml"),
    "/assets/brand/hub-icon-dark.svg": ("assets/brand/hub-icon-dark.svg", "image/svg+xml"),
    "/assets/brand/hub-logo-light.svg": ("assets/brand/hub-logo-light.svg", "image/svg+xml"),
    "/assets/brand/hub-logo-dark.svg": ("assets/brand/hub-logo-dark.svg", "image/svg+xml"),
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
        self.export_slot = threading.BoundedSemaphore(1)
        self.admin = admin
        self.admin_budget = RequestBudget(rate=0.2, burst=5)
        self.fleet = fleet
        self.request_budget = RequestBudget()
        self._requests = threading.BoundedSemaphore(32)
        # An IPv6 literal such as ::1 needs an IPv6 socket; names and IPv4 stay IPv4.
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
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

    def _drain_body(self):
        # A response sent before a declared request body arrives (405, 403, 429...)
        # makes the late body hit a closed socket; Windows then resets the
        # connection and the client loses the response. Read small bodies first.
        if getattr(self, '_body_drained', False):
            return
        self._body_drained = True
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except (AttributeError, TypeError, ValueError):
            return
        if 0 < length <= 65536:
            try:
                self.rfile.read(length)
            except OSError:
                self.close_connection = True

    def end_headers(self):
        self._drain_body()
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
        if path.path in ('/api/data/export', '/api/data/report') and not path.fragment:
            from .data_api import download
            download(self, path.query, report=path.path == "/api/data/report")
        elif path.path == '/api/events' and not path.fragment:
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
        elif path.path == "/api/data/archive/status":
            archive = getattr(self.server, "archive", None)
            self._reply(200, {"schema_version": 1, "archive": archive.snapshot() if archive else {"enabled": False}})
        elif path.path == "/api/history/status":
            history = self.server.fleet.history
            self._reply(200, {"schema_version": 1, "history": history.snapshot() if history else {"enabled": False, "state": "disabled"}})
        elif path.path == '/api/handoff/status':
            handoff = getattr(self.server, 'handoff', None)
            self._reply(200, dict(schema_version=1, handoff=handoff.snapshot() if handoff else {'enabled': False}))
        elif path.path == '/api/replay/status':
            replay = self.server.fleet.replay
            self._reply(200, dict(schema_version=1, replay=replay.snapshot() if replay else {'enabled': False}))
        elif path.path == "/api/admin/nodes":
            if self.server.admin is None:
                self._reply(403, {"error": "admin_disabled", "message": "Web device management is disabled."})
            elif self._check_admin_origin(write=False):
                self._admin_result(self.server.admin.list_nodes)
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

    def _check_admin_origin(self, write=True):
        # Mutation requires exact Origin; local reads allow its absence.
        host = self.headers.get('Host', '').lower()
        suffix = '' if self.server.server_port == 80 else ':' + str(self.server.server_port)
        allowed = ('127.0.0.1' + suffix, 'localhost' + suffix)
        origins = self.headers.get_all('Origin', [])
        if (len(self.headers.get_all('Host', [])) != 1 or host not in allowed or
                (write and len(origins) != 1) or len(origins) > 1 or
                (origins and origins[0] != 'http://' + host) or
                self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin'):
            self._reply(403, {'error': 'untrusted_origin', 'message': 'Use the local Hub page to manage devices.'})
            return False
        # A non-simple header blocks cross-site forms/resource loads; it is not a secret.
        if self.headers.get_all('X-Platmon-Admin', []) != ['1']:
            self._reply(403, {'error': 'untrusted_request', 'message': 'Use the local Devices tab to manage devices.'})
            return False
        if write and not self.server.admin_budget.take():
            self._reply(429, {'error': 'rate_limited', 'message': 'Wait a few seconds before retrying.'})
            return False
        return True

    def _admin_result(self, operation, *args, status=200):
        try:
            self._reply(status, operation(*args))
        except AdminError as exc:
            self._reply(exc.status, {'schema_version': 1, 'error': exc.code, 'message': exc.message})

    def do_POST(self):
        parts = self.path.split('/')
        action, identity = None, None
        if self.path == '/api/admin/nodes':
            action = 'add'
        elif len(parts) == 6 and parts[:4] == ['', 'api', 'admin', 'nodes'] and parts[-1] in ('update', 'remove') and parts[4]:
            action, identity = parts[-1], parts[4]
        if self.server.admin is None or action is None:
            self._read_only()
            return
        if self._request_path() is None or not self._check_admin_origin():
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
            self._body_drained = True
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete body')
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            self._reply(400, {'error': 'invalid_body', 'message': 'Invalid JSON body.'})
            return
        if action == 'add':
            self._admin_result(self.server.admin.add, data, status=201)
        else:
            self._admin_result(getattr(self.server.admin, action), identity, data)

    def _read_only(self):
        self._reply(405, {"error": "read_only"})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _read_only

    def do_HEAD(self):
        # Uptime checkers probe with HEAD. Downloads would run a full export, so refuse them.
        if urlsplit(self.path).path in ('/api/data/export', '/api/data/report'):
            self._read_only()
        else:
            self.do_GET()

    def _body(self, data):
        if self.command != 'HEAD':
            self.wfile.write(data)

    def _static(self, filename, media_type):
        # Exact route-to-file mapping: no user-selected paths or directory traversal.
        data = (ASSET_ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._body(data)

    def _reply(self, status, body):
        output = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(output)))
        self.end_headers()
        self._body(output)
