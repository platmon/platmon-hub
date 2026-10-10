import json
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from hub.registry import Node
from hub.reload import RegistryWatcher
from hub.runtime import Fleet
from test_backend import NODE_ID, FakeClock, FakeClient, st, stats
from test_history import wait_for


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.node = Node(NODE_ID, 'Old', 'http://192.0.2.1:9797')
        self.client = FakeClient()
        self.client.handlers = {'/api/status':st(), '/api/stats':stats()}
        self.fleet = Fleet([self.node], client=self.client)
        self.addCleanup(self.fleet.close)

    def test_rename_preserves_cache_invalid_keeps_registry(self):
        self.fleet.core_once(NODE_ID)
        self.fleet.apply_registry([replace(self.node, name='New')])
        self.assertEqual(self.fleet.get_node(NODE_ID)['name'], 'New')
        self.assertTrue(self.fleet.get_node(NODE_ID)['data_current'])
        with self.assertRaises(ValueError):
            self.fleet.apply_registry([self.node, self.node])
        self.assertEqual(self.fleet.get_node(NODE_ID)['name'], 'New')
        self.fleet.apply_registry([replace(self.node, enabled=False)])
        self.fleet.core_once(NODE_ID)
        self.assertEqual(self.fleet.get_node(NODE_ID)['headline'], 'disabled')
        self.fleet.apply_registry([])
        self.assertIsNone(self.fleet.get_node(NODE_ID))

    def test_late_old_endpoint_response_is_discarded(self):
        entered, release = threading.Event(), threading.Event()
        class Slow:
            def get(self, endpoint, path, deadline):
                if path == '/api/stats':
                    entered.set()
                    if not release.wait(3): raise AssertionError('test release timeout')
                    return stats()
                return st()
        self.fleet.client = Slow()
        thread = threading.Thread(target=self.fleet.core_once, args=(NODE_ID,))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.fleet.apply_registry([replace(self.node, endpoint='http://192.0.2.2:9797')])
        finally:
            release.set()
            thread.join(3)
        self.assertIsNone(self.fleet.get_node(NODE_ID)['sample_sequence'])
        self.assertEqual(self.fleet.get_node(NODE_ID)['node_id'], NODE_ID)

    def test_no_overlap_after_remove_and_readd(self):
        entered, release, new_started = threading.Event(), threading.Event(), threading.Event()
        old_endpoint = self.node.endpoint
        class Slow:
            def get(self, endpoint, path, deadline):
                if endpoint == old_endpoint:
                    entered.set()
                    if not release.wait(3): raise AssertionError('test release timeout')
                else:
                    new_started.set()
                return st() if path == '/api/status' else stats()
        self.fleet.client = Slow()
        self.fleet.start()
        try:
            self.assertTrue(entered.wait(2))
            self.fleet.apply_registry([])
            self.fleet.apply_registry([replace(self.node, endpoint='http://192.0.2.2:9797')])
            with self.fleet._registry_lock:
                self.assertIn(NODE_ID, self.fleet._active)
                self.assertIsNot(self.fleet._active[NODE_ID][2], self.fleet.nodes[NODE_ID])
            self.assertFalse(new_started.is_set())
            release.set()
            self.assertTrue(new_started.wait(2))
        finally:
            release.set()

    def test_debounce_malformed_file_and_restored_last_good(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'nodes.json'
            good = json.dumps({'nodes':[asdict(self.node)]})
            path.write_text(good, encoding='utf-8')
            watcher = RegistryWatcher(path, self.fleet.apply_registry, clock=clock)
            watcher.check_once()
            self.assertEqual(watcher.snapshot()['reloads'], 0)
            clock.advance(1)
            watcher.check_once()
            self.assertEqual(watcher.snapshot()['reloads'], 1)
            path.write_text('{broken', encoding='utf-8')
            watcher.check_once()
            clock.advance(1)
            watcher.check_once()
            self.assertEqual(watcher.snapshot()['state'], 'error')
            self.assertEqual(len(self.fleet.list_nodes()), 1)
            path.write_text(good, encoding='utf-8')
            watcher.check_once()
            watcher.check_once()
            self.assertEqual(watcher.snapshot()['state'], 'watching')
            path.write_text(json.dumps({'nodes':[]}), encoding='utf-8')
            watcher.check_once()
            clock.advance(1)
            watcher.check_once()
            self.assertEqual(self.fleet.list_nodes(), [])

    def test_running_fleet_applies_file_without_restart(self):
        from test_backend import obs
        self.client.handlers['/api/observations'] = obs()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'nodes.json'
            path.write_text('{"nodes":[]}', encoding='utf-8')
            fleet = Fleet([], client=self.client, registry_path=path)
            fleet.start()
            try:
                path.write_text(json.dumps({'nodes':[asdict(self.node)]}), encoding='utf-8')
                wait_for(lambda: fleet.get_node(NODE_ID) is not None and fleet.get_node(NODE_ID)['data_current'], timeout=5)
                self.assertTrue(fleet._scheduler.is_alive())
                self.assertTrue(fleet.watcher._thread.is_alive())
            finally:
                fleet.close()
