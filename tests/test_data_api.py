"""Bounded HTTP downloads generate files without touching live Agents."""
import io
import json
import threading
import unittest
import zipfile
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest.mock import patch

import test_export as fixtures
from hub.data_api import selection
from hub.http import HubServer


class DataApiTests(unittest.TestCase):
    setUp = fixtures.ExportTests.setUp
    tearDown = fixtures.ExportTests.tearDown

    def request(self, query, disabled=False, busy=False):
        history = None if disabled else SimpleNamespace(path=self.db, snapshot=lambda: {'schema_version': 4})
        fleet = SimpleNamespace(history=history)
        server = HubServer(('127.0.0.1', 0), fleet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if busy: server.export_slot.acquire()
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        try:
            conn.request('GET', '/api/data/export?' + query)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()
            if busy: server.export_slot.release()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_zip_from_owned_subprocess(self):
        status, headers, body = self.request('from_ms=1000&to_ms=3000&node_id=' + fixtures.NODE)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/zip')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(json.loads(archive.read('manifest.json'))['node_filter'], [fixtures.NODE])

    def test_validation_history_disabled_and_admission(self):
        for query in ('from_ms=1&to_ms=1', 'from_ms=1&to_ms=2&url=http://example.com',
                      'from_ms=1&from_ms=2&to_ms=3', 'from_ms=0&to_ms=691200000'):
            with self.subTest(query=query): self.assertEqual(self.request(query)[0], 400)
        self.assertEqual(self.request('from_ms=1&to_ms=2', disabled=True)[0], 503)
        self.assertEqual(self.request('from_ms=1&to_ms=2', busy=True)[0], 429)

    def test_worker_failure_is_bounded_and_slot_released(self):
        with patch('hub.data_api.generate', side_effect=OSError('private details')):
            status, _, body = self.request('from_ms=1&to_ms=2')
            self.assertEqual(status, 422)
            self.assertNotIn(b'private details', body)

    def test_selection(self):
        self.assertEqual(selection('from_ms=1&to_ms=2'), (1, 2, None))
        with self.assertRaises(ValueError): selection('from_ms=1&to_ms=2&node_id=bad')
