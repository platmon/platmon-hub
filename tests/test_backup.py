import sqlite3
import tempfile
import unittest
from pathlib import Path
from hub.backup import backup_database
from hub.history.store import HistoryStore
from test_history import record


class BackupTests(unittest.TestCase):
    def test_live_snapshot_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'live.db'
            backup = Path(directory)/'backup space.db'
            store = HistoryStore(path).open()
            try:
                store.insert_batch([record()])
                backup_database(path, backup)
                store.insert_batch([record(2)])
                with self.assertRaises(FileExistsError): backup_database(path, backup)
            finally:
                store.close()
            c = sqlite3.connect(str(backup))
            self.assertEqual(c.execute('SELECT count(*) FROM core_samples').fetchone()[0], 1)
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            c.close()

    def test_missing_source_does_not_leave_a_false_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory)/'backup.db'
            with self.assertRaises(sqlite3.Error): backup_database(Path(directory)/'missing.db', backup)
            self.assertFalse(backup.exists())

    def test_main_shutdown_restores_signal_and_closes_resources(self):
        import signal
        from unittest.mock import patch
        from hub.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            nodes = Path(directory)/'nodes.json'
            nodes.write_text('{"nodes":[]}', encoding='utf-8')
            previous = signal.getsignal(signal.SIGTERM)
            with patch('hub.__main__.Fleet') as fleet, patch('hub.__main__.bind_hub') as bind:
                server = bind.return_value
                server.server_address = ('127.0.0.1', 9787)
                server.serve_forever.side_effect = KeyboardInterrupt
                main(['--nodes',str(nodes)])
                fleet.return_value.start.assert_called_once()
                fleet.return_value.close.assert_called_once()
                server.server_close.assert_called_once()
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
