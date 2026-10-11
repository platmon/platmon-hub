import io
import json
import tempfile
import threading
import time
import unittest
import uuid
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from hub import protocol
from hub.http import HubServer
from hub.registry import Node, load_registry
from hub.runtime import Fleet
from hub.state import NodeState

NODE_ID = str(uuid.UUID(int=1))
INSTANCE = "instance-A"


def st(state="ready", instance=INSTANCE):
    return {"schema_version": 1, "instance_id": instance,
            "ready": state in ("ready", "degraded"), "state": state,
            "sample": {"sequence": 42} if state != "starting" else None}


def stats(seq=42, instance=INSTANCE, age=100, stale=5000):
    return {"schema_version": 1, "sample": {"instance_id": instance,
            "sequence": seq, "data_age_ms": age, "stale_after_ms": stale},
            "collectors": {"core": {"state": "ok"}},
            "cpu": [], "gpu": None, "memory": {"used": 5}, "temperature": {}}


def obs(instance=INSTANCE, groups=None):
    return {"schema_version": 2, "instance_id": instance,
            "groups": {} if groups is None else groups}


class FakeClock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class FakeClient:
    def __init__(self): self.handlers = {}; self.called = []
    def get(self, endpoint, path, deadline):
        self.called.append((endpoint, path))
        response = self.handlers[path]
        if isinstance(response, Exception): raise response
        return response


class RegistryTests(unittest.TestCase):
    def load(self, nodes):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "nodes.json"
            p.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
            return load_registry(p)

    def test_explicit_registration_and_stable_identity(self):
        records = [{"node_id": NODE_ID, "name": "Orin", "endpoint": "http://192.0.2.1:9797"},
                   {"node_id": str(uuid.UUID(int=2)), "name": "Pi", "endpoint": "http://192.0.2.2:9797"}]
        self.assertEqual(self.load(records), self.load(records))
        self.assertEqual(len(self.load(records)), 2)

    def test_bad_uuids_credentials_paths_and_duplicate_endpoints_rejected(self):
        item = {"node_id": NODE_ID, "name": "Orin", "endpoint": "http://192.0.2.1:9797"}
        bad = [dict(item, node_id="bad"), dict(item, endpoint="https://example.com"),
               dict(item, endpoint="http://a:1234/abc"), dict(item, endpoint="http://user:pass@a"),
               dict(item, enabled="yes"), dict(item, name=""),
               dict(item, endpoint="http://localhost:0")]
        for record in bad:
            with self.subTest(record=record), self.assertRaises(ValueError): self.load([record])
        with self.assertRaises(ValueError): self.load([item, dict(item, node_id=str(uuid.uuid4()))])


class ContractTests(unittest.TestCase):
    def test_schema_and_ready_pair(self):
        self.assertEqual(protocol.status(st())["state"], "ready")
        with self.assertRaises(protocol.ProtocolFailure): protocol.status(dict(st(), ready=False))
        with self.assertRaises(protocol.ContractFailure): protocol.status(dict(st(), schema_version=2))
        with self.assertRaises(protocol.ContractFailure): protocol.status(dict(st(), schema_version=True))
        with self.assertRaises(protocol.ProtocolFailure): protocol.status(dict(st(), ready=1))

    def test_core_stats_freshness(self):
        self.assertEqual(protocol.stats(stats(), INSTANCE)["cpu"], [])
        for bad in [dict(stats(), sample=dict(stats()["sample"], data_age_ms=-1)),
                    dict(stats(), sample=dict(stats()["sample"], stale_after_ms=True)),
                    dict(stats(), sample=dict(stats()["sample"], instance_id="wrong"))]:
            with self.subTest(bad=bad), self.assertRaises(protocol.ProtocolFailure):
                protocol.stats(bad, INSTANCE)

    def test_optional_schema2_tri_state(self):
        w = {"state": "ok", "stale_after_ms": 90000, "observation": {"id": 1, "data_age_ms": 50},
             "data": {"connected": None, "connection_state": "unknown"}}
        self.assertIsNone(protocol.observations(obs(groups={"wifi": w}), INSTANCE)["groups"]["wifi"]["data"]["connected"])
        with self.assertRaises(protocol.ContractFailure): protocol.observations(dict(obs(), schema_version=1), INSTANCE)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.node = Node(NODE_ID, "Orin", "http://192.0.2.1:9797")
        self.state = NodeState(self.node, clock=self.clock)

    def test_live_partial_starting_and_stale(self):
        self.state.accept_core(st(), stats())
        self.assertEqual(self.state.snapshot()["headline"], "live")
        self.state.accept_core(st("degraded"), stats())
        self.assertEqual(self.state.snapshot()["headline"], "partial")
        self.state.accept_core(st("starting"))
        self.assertEqual(self.state.snapshot()["headline"], "starting")
        self.state.accept_core(st("stale"))
        self.assertEqual(self.state.snapshot()["headline"], "stale")

    def test_two_failures_and_recovery(self):
        self.state.accept_core(st(), stats())
        self.state.fail_core(protocol.TransportFailure())
        self.assertEqual(self.state.snapshot()["headline"], "stale")
        self.state.fail_core(protocol.TransportFailure())
        self.assertEqual(self.state.snapshot()["headline"], "offline")
        self.state.accept_core(st(), stats())
        self.assertEqual(self.state.snapshot()["headline"], "live")
        self.assertEqual(self.state.snapshot()["consecutive_transport_failures"], 0)

    def test_no_initial_response_and_server_error(self):
        self.state.fail_core(protocol.TransportFailure())
        self.assertEqual(self.state.snapshot()["headline"], "unknown")
        self.state.fail_core(protocol.ServerFailure("500"))
        self.assertEqual(self.state.snapshot()["headline"], "error")

    def test_duplicate_restart_out_of_order(self):
        self.state.accept_core(st(), stats())
        before = self.state.snapshot()["sample_sequence"]
        self.state.accept_core(st(), stats())
        self.assertEqual(self.state.snapshot()["sample_sequence"], before)
        self.state.accept_core(st(), stats(seq=41))
        self.assertEqual(self.state.snapshot()["headline"], "error")
        self.assertEqual(self.state.snapshot()["sample_sequence"], 42)
        self.state.accept_core(st(instance="instance-B"), stats(seq=1, instance="instance-B"))
        self.assertEqual(self.state.snapshot()["sample_sequence"], 1)
        self.assertEqual(self.state.snapshot()["node_id"], NODE_ID)

    def test_freshness_grace_and_receipt_expired(self):
        self.state.accept_core(st(), stats())
        self.clock.advance(5.5)
        self.assertEqual(self.state.snapshot()["headline"], "live")
        self.clock.advance(9)
        self.assertEqual(self.state.snapshot()["headline"], "stale")
        self.state.accept_core(st(), stats(age=6000))
        self.assertEqual(self.state.snapshot()["headline"], "stale")

    def test_reject_out_of_order_observations(self):
        self.state.accept_core(st(), stats())
        current = {"wifi": {"state": "ok", "stale_after_ms": 30000,
                   "observation": {"id": 5, "data_age_ms": 100}, "data": {"connected": None}}}
        older = {"wifi": {"state": "ok", "stale_after_ms": 30000,
                 "observation": {"id": 4, "data_age_ms": 50}, "data": {"connected": False}}}
        self.state.accept_observations(obs(groups=current))
        self.state.accept_observations(obs(groups=older))
        state = self.state.snapshot()
        self.assertEqual(state["optional_error"], "out_of_order_observation")
        self.assertEqual(state["observations"]["wifi"]["observation"]["id"], 5)
        self.assertIsNone(state["observations"]["wifi"]["data"]["connected"])

    def test_optional_failures_and_group_expiry_independent(self):
        self.state.accept_core(st(), stats())
        self.state.accept_observations(obs(groups={"storage": {"state": "ok", "stale_after_ms": 500,
                        "observation": {"id": 1, "data_age_ms": 100}, "data": {"filesystems": []}}}))
        self.clock.advance(1)
        self.assertEqual(self.state.snapshot()["observations"]["storage"]["state"], "stale")
        self.state.fail_observations(protocol.ContractFailure("unsupported"))
        self.assertEqual(self.state.snapshot()["headline"], "live")
        self.assertEqual(self.state.snapshot()["optional_error"], "incompatible")


class PollTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.client.handlers = {"/api/status": st(), "/api/stats": stats(), "/api/observations": obs()}
        self.node = Node(NODE_ID, "Orin", "http://192.0.2.1:9797")
        self.fleet = Fleet([self.node], client=self.client)

    def tearDown(self): self.fleet.close()

    def test_get_only_expected_endpoints_and_optional(self):
        self.fleet.core_once(NODE_ID)
        self.fleet.observations_once(NODE_ID)
        self.assertEqual([p for e, p in self.client.called], ["/api/status", "/api/stats", "/api/observations"])
        self.assertEqual(self.fleet.get_node(NODE_ID)["headline"], "live")

    def test_status_not_ready_skips_stats(self):
        self.client.handlers["/api/status"] = st("stale")
        self.fleet.core_once(NODE_ID)
        self.assertEqual([p for e,p in self.client.called], ["/api/status"])
        self.assertEqual(self.fleet.get_node(NODE_ID)["headline"], "stale")

    def test_status_ready_followed_by_503(self):
        self.client.handlers["/api/stats"] = protocol.NoCurrentData("503")
        self.fleet.core_once(NODE_ID)
        self.assertEqual(self.fleet.get_node(NODE_ID)["headline"], "stale")
        self.assertEqual(self.fleet.get_node(NODE_ID)["consecutive_transport_failures"], 0)

    def test_repeated_stats_timeout_not_offline_with_valid_status(self):
        self.client.handlers["/api/stats"] = protocol.TransportFailure("timeout")
        self.fleet.core_once(NODE_ID)
        self.fleet.core_once(NODE_ID)
        self.assertEqual(self.fleet.get_node(NODE_ID)["headline"], "stale")
        self.assertEqual(self.fleet.get_node(NODE_ID)["consecutive_transport_failures"], 1)
        self.assertEqual(self.fleet.get_node(NODE_ID)["agent_state"], "ready")

    def test_missing_optional_endpoint_is_unsupported_not_alerting(self):
        self.client.handlers["/api/observations"] = protocol.OptionalUnsupported("404")
        self.fleet.core_once(NODE_ID)
        self.fleet.observations_once(NODE_ID)
        node = self.fleet.get_node(NODE_ID)
        self.assertEqual(node["headline"], "live")
        self.assertEqual(node["optional_error"], "unsupported")
        self.assertIsNone(node["optional_alert_error"])
        self.client.handlers["/api/observations"] = protocol.ContractFailure("schema 3")
        self.fleet.observations_once(NODE_ID)
        self.assertIsNone(self.fleet.get_node(NODE_ID)["optional_alert_error"])
        self.client.handlers["/api/observations"] = protocol.ServerFailure("HTTP 500")
        self.fleet.observations_once(NODE_ID)
        self.assertTrue(self.fleet.get_node(NODE_ID)["optional_alert_error"])

    def test_independent_nodes_faults(self):
        healthy = Node(str(uuid.UUID(int=2)), "Pi", "http://192.0.2.2:9797")
        client = FakeClient()
        class PerNode:
            def get(self, endpoint, path, deadline):
                if endpoint == self_bad.endpoint: raise protocol.TransportFailure("timeout")
                return {"/api/status": st(), "/api/stats": stats(), "/api/observations": obs()}[path]
        self_bad = self.node
        other = Fleet([self_bad, healthy], client=PerNode())
        try:
            other.core_once(self_bad.node_id)
            other.core_once(self_bad.node_id)
            other.core_once(healthy.node_id)
            self.assertEqual(other.get_node(self_bad.node_id)["headline"], "offline")
            self.assertEqual(other.get_node(healthy.node_id)["headline"], "live")
        finally: other.close()

    def test_scheduler_more_nodes_than_workers(self):
        nodes = [Node(str(uuid.UUID(int=i + 1)), str(i), f"http://192.0.2.{i+1}:9797") for i in range(4)]
        hits = set()
        lock = threading.Lock()
        class Many:
            def get(self, endpoint, path, deadline):
                with lock: hits.add(endpoint)
                return {"/api/status": st(), "/api/stats": stats(), "/api/observations": obs()}[path]
        fleet = Fleet(nodes, client=Many(), max_concurrency=2, core_interval=0.02, observations_interval=0.02)
        expected = {n.endpoint for n in nodes}
        try:
            fleet.start()
            # Wait for actual observations rather than assuming 300 ms scheduling
            # on all hosts (especially busy Windows CI runners).
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with lock:
                    if hits == expected:
                        break
                time.sleep(0.02)
            with lock:
                self.assertEqual(hits, expected)
        finally: fleet.close()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.fleet = Fleet([Node(NODE_ID, "Orin", "http://192.0.2.1:9797")], client=FakeClient())
        self.server = HubServer(("127.0.0.1", 0), self.fleet)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fleet.close()
        self.thread.join(timeout=1)

    def request(self, method, path):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        conn.request(method, path)
        reply = conn.getresponse()
        status = reply.status
        body = json.loads(reply.read())
        conn.close()
        return status, body

    def test_read_api_and_methods(self):
        self.assertEqual(self.request("GET", "/healthz"), (200, {"status": "ok"}))
        self.assertEqual(self.request("GET", "/api/nodes")[1]["nodes"][0]["node_id"], NODE_ID)
        self.assertEqual(self.request("GET", "/api/nodes/" + NODE_ID)[0], 200)
        self.assertEqual(self.request("GET", "/api/nodes/does-not-exist")[0], 404)
        self.assertEqual(self.request("POST", "/api/nodes")[0], 405)
        self.assertEqual(self.request("GET", "/api/proxy?url=http://example.com")[0], 404)


if __name__ == "__main__": unittest.main()

class HttpAgentClientTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/stats":
                    data = b"{" + b" " * 1024
                    self.send_response(200)
                elif self.path == "/api/observations":
                    data = b'{"schema_version":2,"groups":{},"value":NaN}'
                    self.send_response(200)
                elif self.path == "/api/status":
                    data = b""
                    self.send_response(302)
                    self.send_header("Location", "http://example.com/")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, format, *args): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def test_disallowed_path_bounded_response_redirect_and_nan(self):
        client = protocol.HttpAgentClient(max_bytes=128)
        with self.assertRaises(ValueError): client.get(self.endpoint, "/api/proxy", 2)
        with self.assertRaises(protocol.ProtocolFailure): client.get(self.endpoint, "/api/stats", 2)
        with self.assertRaises(protocol.ProtocolFailure): client.get(self.endpoint, "/api/status", 2)
        with self.assertRaises(protocol.ProtocolFailure): client.get(self.endpoint, "/api/observations", 2)

    def test_optional_404_is_unsupported_but_core_404_is_error(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Missing(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, format, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Missing)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = "http://127.0.0.1:" + str(server.server_address[1])
        client = protocol.HttpAgentClient()
        with self.assertRaises(protocol.OptionalUnsupported): client.get(endpoint, "/api/observations", 2)
        with self.assertRaises(protocol.ServerFailure): client.get(endpoint, "/api/status", 2)
