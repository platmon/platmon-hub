import contextlib
import io
import json
import os
import tempfile
import threading
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

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing violation')
    def test_registry_open_elsewhere_briefly_is_retried(self):
        identity = self.add()
        reader = self.path.open('rb')
        release = threading.Timer(0.2, reader.close)
        release.start()
        self.addCleanup(release.join)
        self.addCleanup(reader.close)
        update_registry(self.path, 'rename', node_id=identity, name='Renamed while open')
        self.assertEqual(load_registry(self.path)[0].name, 'Renamed while open')

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

    def run_add(self, options, answers=()):
        output, errors = io.StringIO(), io.StringIO()
        with patch('builtins.input', side_effect=answers), patch('urllib.request.urlopen') as request, \
                patch('hub.__main__.Fleet') as fleet, contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(errors):
            main(['nodes', '--file', str(self.path), 'add'] + options)
        request.assert_not_called()
        fleet.assert_not_called()
        return json.loads(output.getvalue()), errors.getvalue()

    def test_host_default_custom_port_and_ipv6(self):
        for host, options, endpoint in (
            ('192.0.2.10', [], 'http://192.0.2.10:9797'),
            ('lab.example', ['--port', '12345'], 'http://lab.example:12345'),
            ('::1', [], 'http://[::1]:9797'),
            ('[2001:db8::1]', ['--port', '12345'], 'http://[2001:db8::1]:12345'),
        ):
            with self.subTest(host=host):
                result, _ = self.run_add(['--name', 'Lab', '--host', host] + options)
                node = next(n for n in load_registry(self.path) if n.node_id == result['node_id'])
                self.assertEqual(node.endpoint, endpoint)
                self.assertTrue(node.enabled)

    def test_interactive_confirmation_and_machine_output(self):
        result, preview = self.run_add(['--interactive', '--disabled'], ['Lab', '192.0.2.10', '', 'yes'])
        self.assertEqual(result['action'], 'add')
        node = load_registry(self.path)[0]
        self.assertEqual(node.name, 'Lab')
        self.assertEqual(node.endpoint, 'http://192.0.2.10:9797')
        self.assertFalse(node.enabled)
        self.assertIn('http://192.0.2.10:9797', preview)

    def test_interactive_cancel_or_eof_never_changes_files(self):
        self.add()
        original = self.path.read_bytes()
        lock = Path(str(self.path) + '.lock')
        lock_modified = lock.stat().st_mtime_ns
        for answers, code in ((['Other', '192.0.2.2', '', ''], 0),
                              (['Other', '192.0.2.2', '', 'no'], 0),
                              ([EOFError()], 2), ([KeyboardInterrupt()], 2)):
            with self.subTest(answers=answers), self.assertRaises(SystemExit) as result:
                self.run_add(['--interactive'], answers)
            self.assertEqual(result.exception.code, code)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(Path(str(self.path) + '.bak').exists())
            self.assertEqual(lock.stat().st_mtime_ns, lock_modified)

    def test_invalid_host_and_option_combinations_do_not_write(self):
        cases = [
            ['--name', 'Lab'], ['--host', '192.0.2.10'],
            ['--name', 'Lab', '--host', '192.0.2.10:9797'],
            ['--name', 'Lab', '--host', 'http://192.0.2.10'],
            ['--name', 'Lab', '--host', 'user:secret@192.0.2.10'],
            ['--name', 'Lab', '--host', '192.0.2.10/path'],
            ['--name', 'Lab', '--host', '192.0.2.10', '--port', '0'],
            ['--name', 'Lab', '--host', '192.0.2.10', '--port', '65536'],
            ['--name', 'Lab', '--endpoint', 'http://192.0.2.10:9797', '--port', '12345'],
            ['--name', 'Lab', '--host', '192.0.2.10', '--endpoint', 'http://192.0.2.11:9797'],
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(SystemExit) as result:
                self.run_add(options)
            self.assertEqual(result.exception.code, 2)
            self.assertFalse(self.path.exists())

    def test_duplicate_host_guidance_preserves_original(self):
        self.add()
        original = self.path.read_bytes()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main(['nodes', '--file', str(self.path), 'add', '--name', 'Duplicate', '--host', '192.0.2.1'])
        self.assertEqual(result.exception.code, 2)
        self.assertIn('already exists', errors.getvalue())
        self.assertIn('set-endpoint', errors.getvalue())
        self.assertEqual(self.path.read_bytes(), original)

    def test_credential_errors_do_not_echo_secret_or_preview(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            main(['nodes', '--file', str(self.path), 'add', '--interactive', '--name', 'Lab',
                  '--endpoint', 'http://user:private-token@192.0.2.10:9797'])
        self.assertNotIn('private-token', errors.getvalue())
        self.assertNotIn('user:', errors.getvalue())
        self.assertNotIn('Save this device', errors.getvalue())
        self.assertIn('without credentials', errors.getvalue())
        self.assertFalse(self.path.exists())

    def test_missing_parent_guidance_and_interactive_custom_port(self):
        result, _ = self.run_add(['--interactive', '--name', 'Lab'], ['192.0.2.10', '12345', 'yes'])
        self.assertEqual(load_registry(self.path)[0].endpoint, 'http://192.0.2.10:12345')
        self.assertEqual(result['action'], 'add')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            main(['nodes', '--file', str(self.path.parent / 'missing' / 'nodes.json'),
                  'add', '--name', 'Lab', '--host', '192.0.2.10'])
        self.assertIn('parent directory', errors.getvalue())
