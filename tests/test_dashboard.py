"""Fleet dashboard contract checks. HTTP tests use loopback only (no device access)."""

import hashlib
import json
import threading
import unittest
import uuid
from http.client import HTTPConnection
from pathlib import Path

from hub.http import ASSET_ROOT, HubServer
from hub.registry import Node
from hub.runtime import Fleet
from hub.state import NodeState

NODE_ID = str(uuid.UUID(int=1))
# Exact Git blob hashes of platmon's canonical C / Terminal P assets.
CANONICAL_SVG_BLOBS = {
    "platmon-icon-light.svg": "23c7ea4163fbce524b9886fdc56d6b4edf4fa501",
    "platmon-icon-dark.svg": "52a3b33fadb3b3e22b7e795d9631b9406578763c",
    "platmon-logo-light.svg": "1da0ba8c2e466129a359f3f4b6c2178e13cd06bc",
    "platmon-logo-dark.svg": "333a1fb418491adf344539f2b5b46ea937a194c5",
    "favicon.svg": "1537abf9eca4460600edbec91e037748fbdfb5a2",
}



class Clock:
    def __init__(self):
        self.tick = 100.0

    def __call__(self):
        return self.tick


def fixture():
    return {
        "schema_version": 1,
        "sample": {"instance_id": "process-A", "sequence": 5,
                   "data_age_ms": 200, "stale_after_ms": 5000},
        "collectors": {"core": {"state": "ok"}},
        "cpu": [{"id": 0, "usage": 20.0}, {"id": 1, "usage": 40.0}],
        "gpu": None,
        "memory": {"used": 250, "total": 1000},
        "disk": {"used": 10, "total": 20},
        "temperature": {"safe": 30.1, "</script><img src=x onerror=alert(1)>": 52.2},
    }


def fixture_status():
    return {"schema_version": 1, "instance_id": "process-A",
            "state": "ready", "ready": True, "sample": {"sequence": 5}}


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = NodeState(Node(NODE_ID, "<malicious>name", "http://192.0.2.1:9797"),
                               clock=self.clock)
        self.state.accept_core(fixture_status(), fixture())

    def test_aggregate_percentages_and_missing_gpu(self):
        snap = self.state.snapshot(include_details=False)
        self.assertEqual(snap["overview"], {
            "cpu_percent": 30.0, "gpu_percent": None, "memory_percent": 25.0,
            "disk_percent": 50.0, "hottest_c": 52.2
        })
        self.assertIsNone(snap["stats"])
        self.assertTrue(snap["data_current"])

    def test_expired_last_known_and_bad_numbers(self):
        self.clock.tick += 20
        snap = self.state.snapshot(include_details=False)
        self.assertEqual(snap["headline"], "stale")
        self.assertFalse(snap["data_current"])
        self.assertEqual(snap["overview"]["cpu_percent"], 30.0)
        other = NodeState(Node(str(uuid.uuid4()), "Pi", "http://192.0.2.2:9797"), clock=self.clock)
        s = fixture()
        s["cpu"] = []
        s["memory"] = {"used": 11, "total": 0}
        s["disk"] = {"used": -4, "total": 100}
        s["gpu"] = {"usage": "unknown"}
        s["temperature"] = {"invalid": None}
        other.accept_core(fixture_status(), s)
        self.assertEqual(other.snapshot()["overview"], {
            "cpu_percent": None, "gpu_percent": None, "memory_percent": None,
            "disk_percent": None, "hottest_c": None
        })

    def test_disabled_node_cannot_be_current(self):
        disabled = NodeState(Node(str(uuid.uuid4()), "Off", "http://192.0.2.3:9797", False),
                             clock=self.clock)
        disabled.accept_core(fixture_status(), fixture())
        snap = disabled.snapshot()
        self.assertEqual(snap["headline"], "disabled")
        self.assertFalse(snap["data_current"])
        self.assertFalse(snap["ready"])


class DashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.fleet = Fleet([Node(NODE_ID, "<img src=x onerror=alert(1)>",
                                "http://192.0.2.1:9797")])
        self.fleet.nodes[NODE_ID].accept_core(fixture_status(), fixture())
        self.server = HubServer(("127.0.0.1", 0), self.fleet)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fleet.close()
        self.thread.join(timeout=2)

    def request(self, method, path):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_static_allowlist_mime_csp_and_no_query(self):
        for path, mime in [("/", "text/html"), ("/index.html", "text/html"),
                           ("/assets/styles.css", "text/css"),
                           ("/assets/app.js", "text/javascript"),
                           ("/favicon.svg", "image/svg+xml"),
                           ("/assets/brand/platmon-icon-light.svg", "image/svg+xml"),
                           ("/assets/brand/platmon-icon-dark.svg", "image/svg+xml"),
                           ("/assets/brand/platmon-logo-light.svg", "image/svg+xml"),
                           ("/assets/brand/platmon-logo-dark.svg", "image/svg+xml")]:
            with self.subTest(path=path):
                status, headers, data = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(mime, headers["Content-Type"])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertTrue(data)
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertIn("img-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/assets/../registry.py")[0], 404)
        self.assertEqual(self.request("GET", "/assets/brand/brand-colors.json")[0], 404)
        self.assertEqual(self.request("GET", "/assets/brand/README.md")[0], 404)
        self.assertEqual(self.request("GET", "/assets/brand/missing.svg")[0], 404)
        self.assertEqual(self.request("GET", "/favicon.svg?x=1")[0], 404)
        self.assertEqual(self.request("GET", "/assets/app.js?target=x")[0], 404)
        self.assertEqual(self.request("GET", "/api/proxy?url=http://example.com")[0], 404)
        self.assertEqual(self.request("POST", "/")[0], 405)

    def test_brand_svg_assets_are_exact_canonical_blobs(self):
        for filename, expected_git_sha in CANONICAL_SVG_BLOBS.items():
            with self.subTest(filename=filename):
                original = (ASSET_ROOT / "assets" / "brand" / filename).read_bytes()
                sha = hashlib.sha1(b"blob " + str(len(original)).encode("ascii") +
                                   b"\x00" + original).hexdigest()
                self.assertEqual(sha, expected_git_sha)
                route = "/favicon.svg" if filename == "favicon.svg" else "/assets/brand/" + filename
                status, headers, served = self.request("GET", route)
                self.assertEqual(status, 200)
                self.assertEqual(served, original)
                self.assertEqual(headers["Content-Type"], "image/svg+xml")

    def test_document_references_light_dark_assets_not_text_logo(self):
        status, headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        document = data.decode("utf-8")
        self.assertIn('rel="icon" type="image/svg+xml" href="/favicon.svg"', document)
        self.assertIn('/assets/brand/platmon-icon-light.svg', document)
        self.assertIn('/assets/brand/platmon-icon-dark.svg', document)
        self.assertIn('prefers-color-scheme: dark', document)
        self.assertNotIn('class="brand-mark" aria-hidden="true">p_', document)
        self.assertNotIn('class="brand-label"', document)
        self.assertNotIn('platmon <strong>hub</strong>', document)
        self.assertIn('<img src="/assets/brand/platmon-icon-light.svg" alt="platmon"', document)
        self.assertIn('<title>Fleet monitoring</title>', document)
        self.assertNotIn("platmon-hub · Cached fleet view", document)
        self.assertIn('<span class="product-tag">Fleet monitoring</span>', document)

    def test_fleet_monitoring_is_prominent_and_visible_on_mobile(self):
        status, headers, data = self.request("GET", "/assets/styles.css")
        self.assertEqual(status, 200)
        css = data.decode("utf-8")
        self.assertIn(".product-tag {color:var(--fg);font-size:24px;font-weight:700;", css)
        self.assertIn(".product-tag{font-size:17px;", css)
        self.assertNotIn(".product-tag,.topbar-caption{display:none;}", css)
        self.assertIn(".topbar-caption{display:none;}", css)

    def test_fleet_summary_is_compact_and_detail_is_cached(self):
        status, headers, data = self.request("GET", "/api/nodes")
        self.assertEqual(status, 200)
        response = json.loads(data)
        self.assertEqual(response["schema_version"], 1)
        node = response["nodes"][0]
        self.assertEqual(node["overview"]["cpu_percent"], 30.0)
        self.assertIsNone(node["stats"])
        self.assertTrue(node["data_current"])
        status, _, data = self.request("GET", "/api/nodes/" + NODE_ID)
        self.assertEqual(status, 200)
        detail = json.loads(data)["node"]
        self.assertIn("</script>", list(detail["stats"]["temperature"])[1])
        self.assertEqual(detail["overview"]["memory_percent"], 25.0)

    def test_browser_script_does_not_interpolate_device_html(self):
        status, _, source = self.request("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        js = source.decode("utf-8")
        self.assertIn("textContent", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("document.hidden", js)
        self.assertIn('"/api/nodes/"', js)
        self.assertNotIn("http://192.0.2.1", js)


if __name__ == "__main__":
    unittest.main()
