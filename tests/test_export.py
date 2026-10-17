"""Export real SQLite fixtures without accessing any Agent."""
import csv
import hashlib
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from hub.export import ExportError, export_history, spreadsheet_cell
from hub.history.store import HistoryStore

NODE = str(uuid.UUID(int=1))
OTHER = str(uuid.UUID(int=2))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'history.db'
        self.store = HistoryStore(self.db).open()
        c = self.store.connection
        for node, instance, sequence, received, cpu, gpu in [
                (NODE, 'first', 1, 1000, 0, None),
                (NODE, '=formula', 1, 2000, 90, 20),
                (OTHER, 'other', 1, 3000, 10, 10)]:
            c.execute('INSERT INTO core_samples VALUES (' + ','.join('?' * 20) + ')',
                      (node, instance, sequence, received, 0, 5000, 'ready', cpu, gpu, 30, 40, 50, None) + (None,) * 7)
        c.execute('INSERT INTO events (node_id,event_type,received_at_ms,previous_state,current_state,reason_code,event_id) VALUES (?,?,?,?,?,?,?)',
                  (NODE, 'core_state', 1500, 'live', 'offline', 'core_transport', 'event-1'))
        c.execute('INSERT INTO gaps (node_id,scope,instance_id,start_ms,end_ms,reason) VALUES (?,?,?,?,?,?)',
                  (NODE, 'core', 'first', 500, None, 'unobserved'))
        c.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)',
                  (NODE, 'first', 'probe', 1, 1200, 0, 10000, 'ok', '{}'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def export(self, **kwargs):
        return export_history(self.db, self.root / 'export.zip', 1000, 3000, **kwargs)

    def rows(self, archive, name):
        return list(csv.DictReader(io.StringIO(archive.read(name).decode('utf-8-sig'))))

    def test_raw_identity_null_zero_half_open_and_hashes(self):
        result = self.export()
        with zipfile.ZipFile(self.root / 'export.zip') as z:
            manifest = json.loads(z.read('manifest.json'))
            self.assertEqual(manifest, result)
            self.assertFalse(manifest['observations_included'])
            core = next(f['name'] for f in result['files'] if f['name'].startswith('measurements/'))
            rows = self.rows(z, core)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['cpu_percent'], '0.0')
            self.assertEqual(rows[0]['gpu_percent'], '')
            self.assertEqual(rows[1]['instance_id'], "'=formula")
            self.assertEqual(rows[0]['sequence'], rows[1]['sequence'])
            self.assertEqual(len(self.rows(z, 'gaps.csv')), 1)
            self.assertEqual(self.rows(z, 'gaps.csv')[0]['end_ms'], '')
            for info in result['files']:
                self.assertEqual(hashlib.sha256(z.read(info['name'])).hexdigest(), info['sha256'])
            self.assertNotIn('observations.csv', z.namelist())

    def test_filter_deleted_identity_and_observations(self):
        # There is deliberately no registry: removed device data stays exportable.
        result = self.export(node_ids=[NODE], observations=True)
        self.assertEqual(result['node_filter'], [NODE])
        with zipfile.ZipFile(self.root / 'export.zip') as z:
            self.assertEqual(len(self.rows(z, 'observations.csv')), 1)

    def test_consistent_snapshot_with_concurrent_commit(self):
        original = spreadsheet_cell
        committed = []
        def inject(value):
            if not committed:
                committed.append(True)
                self.store.connection.execute('INSERT INTO events (node_id,event_type,received_at_ms,previous_state,current_state,reason_code,event_id) VALUES (?,?,?,?,?,?,?)',
                    (NODE, 'core_state', 1600, 'offline', 'live', 'current', 'event-new'))
            return original(value)
        with patch('hub.export.spreadsheet_cell', side_effect=inject):
            self.export()
        with zipfile.ZipFile(self.root / 'export.zip') as z:
            self.assertEqual(len(self.rows(z, 'events.csv')), 1)

    def test_empty_archive(self):
        result = self.export(node_ids=[str(uuid.UUID(int=3))])
        self.assertEqual(sum(f['rows'] for f in result['files']), 0)

    def test_failure_never_publishes_partial_or_replaces(self):
        for options in ({'max_rows': 1}, {'max_bytes': 1}):
            with self.subTest(options=options):
                with self.assertRaises(ExportError):
                    self.export(**options)
                self.assertFalse((self.root / 'export.zip').exists())
                self.assertFalse(list(self.root.glob('.platmon-export-*')))
        (self.root / 'export.zip').write_bytes(b'original')
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual((self.root / 'export.zip').read_bytes(), b'original')

    def test_destination_race_refuses_replacement(self):
        original = __import__('os').link
        def race(source, destination):
            Path(destination).write_bytes(b'competitor')
            original(source, destination)
        with patch('hub.export.os.link', side_effect=race):
            with self.assertRaises(FileExistsError):
                self.export()
        self.assertEqual((self.root / 'export.zip').read_bytes(), b'competitor')
        self.assertFalse(list(self.root.glob('.platmon-export-*')))

    def test_invalid_ranges_and_schema(self):
        for start, end in ((-1, 1), (1, 1), (0, 32 * 86400000), (True, 100)):
            with self.assertRaises(ExportError):
                export_history(self.db, self.root / 'no.zip', start, end)
        with self.assertRaises(ExportError):
            self.export(node_ids=['../../anything'])
        self.store.connection.execute('PRAGMA user_version=99')
        with self.assertRaises(ExportError):
            self.export()
        self.assertFalse((self.root / 'export.zip').exists())

    def test_spreadsheet_injection_and_negative_numbers(self):
        for value in ('=SUM(1,2)', '+cmd', '-cmd', '@SUM', ' \t=cmd', '\ncmd', "'literal"):
            self.assertEqual(spreadsheet_cell(value), "'" + value)
        self.assertEqual(spreadsheet_cell(-10.5), -10.5)
        self.assertEqual(spreadsheet_cell(None), None)

    def test_deadline_cleans_temporary_file(self):
        with patch('hub.export.time.monotonic', side_effect=[0, 31]):
            with self.assertRaises(ExportError):
                self.export()
        self.assertFalse((self.root / 'export.zip').exists())
        self.assertFalse(list(self.root.glob('.platmon-export-*')))
