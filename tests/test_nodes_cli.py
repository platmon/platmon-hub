import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub.__main__ import main
from hub.filelock import FileLock
from hub.nodes_cli import update_registry
from hub.registry import load_registry


class NodesCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'nodes.json'

    def add(self):
        return update_registry(self.path, 'add', name='Synthetic node', endpoint='http://192.0.2.1:9797')

    def test_lifecycle_identity_and_history_untouched(self):
        identity = self.add()
        history = self.path.with_name('history.db')
        history.write_bytes(b'untouched')
        update_registry(self.path, 'rename', node_id=identity, name='Renamed')
        update_registry(self.path, 'set-endpoint', node_id=identity, endpoint='http://192.0.2.2:9797')
        update_registry(self.path, 'disable', node_id=identity)
        self.assertFalse(load_registry(self.path)[0].enabled)
        update_registry(self.path, 'enable', node_id=identity)
        self.assertEqual(load_registry(self.path)[0].node_id, identity)
        update_registry(self.path, 'remove', node_id=identity)
        self.assertEqual(load_registry(self.path), ())
        self.assertEqual(history.read_bytes(), b'untouched')
        self.assertEqual(load_registry(str(self.path)+'.bak')[0].node_id, identity)

    def test_invalid_duplicate_and_conflict_keep_original(self):
        self.add()
        original = self.path.read_bytes()
        with self.assertRaises(ValueError): self.add()
        with self.assertRaises(ValueError):
            update_registry(self.path, 'add', name='bad', endpoint='http://user:secret@192.0.2.3:9797')
        with FileLock(str(self.path)+'.lock'):
            with self.assertRaises(OSError):
                update_registry(self.path, 'add', name='busy', endpoint='http://192.0.2.2:9797')
        self.assertEqual(self.path.read_bytes(), original)

    def test_atomic_replace_failure_preserves_original_and_backup(self):
        identity = self.add()
        original = self.path.read_bytes()
        import os
        real = os.replace
        def fail(source, destination):
            if Path(destination).resolve() == self.path.resolve():
                raise PermissionError('injected file lock')
            return real(source, destination)
        with patch('hub.nodes_cli.os.replace', side_effect=fail):
            with self.assertRaises(PermissionError):
                update_registry(self.path, 'rename', node_id=identity, name='Lost')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(Path(str(self.path)+'.bak').read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('.nodes.json-*')), [])

    def test_cli_routes_without_starting_fleet(self):
        with patch('hub.__main__.Fleet') as fleet, contextlib.redirect_stdout(io.StringIO()) as output:
            main(['nodes','--file',str(self.path),'add','--name','CLI','--endpoint','http://192.0.2.1:9797'])
            main(['nodes','--file',str(self.path),'validate'])
            main(['nodes','--file',str(self.path),'list'])
        fleet.assert_not_called()
        self.assertIn('"valid": true', output.getvalue())
        self.assertEqual(len(load_registry(self.path)), 1)

    def test_malformed_registry_is_not_replaced(self):
        self.path.write_text('{bad', encoding='utf-8')
        with self.assertRaises(ValueError): self.add()
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{bad')
