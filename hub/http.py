"""Loopback-default read-only Hub JSON endpoints."""

import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


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

    def __init__(self, address, fleet):
        self.fleet = fleet
        super().__init__(address, HubHandler)


class HubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path)
        if path.query or path.fragment:
            self._reply(404, {"error": "not_found"})
        elif path.path in ASSETS:
            self._static(*ASSETS[path.path])
        elif path.path == "/healthz":
            self._reply(200, {"status": "ok"})
        elif path.path == "/api/nodes":
            self._reply(200, {"schema_version": 1, "nodes": self.server.fleet.list_nodes()})
        elif path.path.startswith("/api/nodes/") and path.path.count("/") == 3:
            node = self.server.fleet.get_node(path.path.split("/")[-1])
            self._reply(200, {"schema_version": 1, "node": node}) if node is not None else self._reply(404, {"error": "not_found"})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self):
        self._reply(405, {"error": "read_only"})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def _static(self, filename, media_type):
        # Exact route-to-file mapping: no user-selected paths or directory traversal.
        data = (ASSET_ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
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
