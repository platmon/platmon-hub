"""Schema 7: pressure and I/O rates from platmon /api/stats, kept for history only.

Shapes follow platmon docs/reference/pressure.md, network.md and disk-io.md; values are synthetic.
"""
import copy
import csv
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hub.export import export_history
from hub.history.projector import LOAD_COLUMNS
from hub.history.query import query_history
from hub.history.schema import SQL, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5, MIGRATION_6, VERSION
from hub.history.store import HistoryStore
from hub.registry import Node
from hub.runtime import Fleet
from hub.state import _load
from test_agent_v120 import fixture
from test_backend import FakeClient, FakeClock, NODE_ID
from test_history import record


def interface(name, rx, tx):
    return {'name': name, 'ifindex': 1, 'rx': {}, 'tx': {}, 'window_ms': 1000.0, 'reason': None,
            'rates': {'rx_bytes_per_s': rx, 'tx_bytes_per_s': tx, 'rx_packets_per_s': 0.0, 'tx_packets_per_s': 0.0}}


def disk(name, read, write):
    return {'name': name, 'major': 259, 'minor': 0, 'window_ms': 1000.0, 'reason': None,
            'rates': {'read_bytes_per_s': read, 'write_bytes_per_s': write, 'reads_per_s': 1.0, 'writes_per_s': 1.0}}


def load_stats():
    some = lambda avg10: {'avg10': avg10, 'avg60': 0.0, 'avg300': 0.0, 'total_us': 1}
    return {
        'pressure': {'scope': {'kind': 'host'}, 'provider': 'proc_pressure', 'read': None,
                     'resources': {'cpu': {'some': some(1.25), 'full': None},
                                   'memory': {'some': some(0.0), 'full': some(0.0)},
                                   'io': {'some': some(3.0), 'full': some(0.5)}}},
        'network': {'provider': 'proc_net_dev', 'read': None, 'interfaces': [
            interface('br0', 900.0, 100.0), interface('eth0', 900.0, 100.0),
            interface('lo', 50000.0, 50000.0), interface('wlan0', 10.0, 300.0)]},
        'disk_io': {'provider': 'proc_diskstats', 'read': None, 'disks': [
            disk('mmcblk0', 4096.0, 0.0), disk('nvme0n1', 1024.0, 8192.0)]},
    }


class LoadProjectionTests(unittest.TestCase):
    def test_projection_values(self):
        self.assertEqual(_load(load_stats()), {
            'psi_cpu_some_avg10': 1.25, 'psi_memory_some_avg10': 0.0, 'psi_io_some_avg10': 3.0,
            # Busiest non-loopback interface per direction; bridge and port are not added.
            'network_rx_busiest_bytes_per_s': 900.0, 'network_tx_busiest_bytes_per_s': 300.0,
            'disk_read_bytes_per_s': 5120.0, 'disk_write_bytes_per_s': 8192.0})

    def test_absent_and_partial_evidence_stays_null(self):
        self.assertEqual(_load(None), dict.fromkeys(LOAD_COLUMNS))
        self.assertEqual(_load({}), dict.fromkeys(LOAD_COLUMNS))
        stats = load_stats()
        stats['pressure']['resources'] = {'io': stats['pressure']['resources']['io']}
        stats['network']['interfaces'] = [interface('lo', 5.0, 5.0), dict(interface('eth0', 1.0, 1.0), rates=None)]
        # One disk still warming up leaves the total unknown rather than undercounted.
        stats['disk_io']['disks'][0]['rates'] = None
        projected = _load(stats)
        self.assertEqual(projected['psi_io_some_avg10'], 3.0)
        for name in LOAD_COLUMNS:
            if name != 'psi_io_some_avg10':
                self.assertIsNone(projected[name], name)
        stats = load_stats()
        stats['pressure']['resources']['cpu']['some']['avg10'] = 101
        stats['network']['interfaces'][0]['rates']['rx_bytes_per_s'] = float('nan')
        stats['network']['interfaces'][1]['rates']['rx_bytes_per_s'] = -1
        stats['disk_io']['disks'][1]['rates']['write_bytes_per_s'] = True
        projected = _load(stats)
        self.assertIsNone(projected['psi_cpu_some_avg10'])
        self.assertEqual(projected['network_rx_busiest_bytes_per_s'], 10.0)
        self.assertIsNone(projected['disk_write_bytes_per_s'])
        self.assertIsNone(_load(dict(load_stats(), disk_io={'disks': []}))['disk_read_bytes_per_s'])

    def test_record_rejects_out_of_range_values(self):
        self.assertTrue(replace(record(), psi_cpu_some_avg10=100, disk_read_bytes_per_s=0).valid())
        for name, value in (('psi_io_some_avg10', 100.5), ('psi_cpu_some_avg10', -1),
                            ('network_rx_busiest_bytes_per_s', -0.5), ('disk_write_bytes_per_s', float('inf'))):
            self.assertFalse(replace(record(), **{name: value}).valid(), name)


class LoadHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'history.db'

    def test_poll_history_query_and_export(self):
        store = HistoryStore(self.path).open()
        self.addCleanup(store.close)
        outer = self
        class Sink:
            def submit(self, item):
                if item is not None:
                    outer.assertTrue(item.valid())
                    store.insert_batch([item])
            def close(self): pass
        document = fixture()
        document['stats'].update(load_stats())
        client = FakeClient()
        client.handlers = {'/api/' + name: data for name, data in document.items()}
        node = Node(NODE_ID, 'Synthetic load host', 'http://192.0.2.1:9797')
        fleet = Fleet([node], client=client, history=Sink(), clock=FakeClock())
        self.addCleanup(fleet.close)
        fleet.core_once(NODE_ID)
        when = store.connection.execute('SELECT received_at_ms FROM core_samples').fetchone()[0]
        points = query_history(self.path, NODE_ID, 'disk_read_bytes_per_s', when, when + 10000, 100)['points']
        self.assertEqual(points[0]['average'], 5120.0)
        points = query_history(self.path, NODE_ID, 'psi_io_some_avg10', when, when + 10000, 100)['points']
        self.assertEqual(points[0]['average'], 3.0)
        # The node card summary is unchanged: these values are history-only.
        self.assertNotIn('psi_io_some_avg10', fleet.list_nodes()[0]['overview'])
        out = self.path.with_suffix('.zip')
        export_history(self.path, out, when, when + 10000, reports=True)
        with zipfile.ZipFile(out) as z:
            rows = lambda name: list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))
            core = rows(next(n for n in z.namelist() if n.startswith('measurements/')))[0]
            self.assertEqual(core['network_tx_busiest_bytes_per_s'], '300.0')
            summary = rows('summary-daily.csv')[0]
            self.assertEqual(summary['disk_write_bytes_per_s_maximum'], '8192.0')
            units = json.loads(z.read('manifest.json'))['units']
            self.assertEqual(units['disk_read_bytes_per_s'], 'bytes per second')
            self.assertEqual(units['psi_cpu_some_avg10'], 'percent')

    def old_database(self):
        with closing(sqlite3.connect(str(self.path))) as c, c:
            c.executescript(SQL + MIGRATION_2 + MIGRATION_3 + MIGRATION_4 + MIGRATION_5 + MIGRATION_6)
            c.execute('INSERT INTO core_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', record().values()[:13])

    def test_v6_upgrade_preserves_rows_and_verified_backup(self):
        self.old_database()
        store = HistoryStore(self.path).open()
        try:
            c = store.connection
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], VERSION)
            self.assertEqual(c.execute('SELECT cpu_percent,' + ','.join(LOAD_COLUMNS) + ' FROM core_samples').fetchone(),
                             (25,) + (None,) * len(LOAD_COLUMNS))
            store.insert_batch([replace(record(2), psi_io_some_avg10=4.5, disk_read_bytes_per_s=10.0)])
            self.assertEqual(c.execute('SELECT psi_io_some_avg10,disk_read_bytes_per_s FROM core_samples WHERE sequence=2').fetchone(),
                             (4.5, 10.0))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE core_samples SET network_rx_busiest_bytes_per_s=-1")
        finally:
            store.close()
        backup = next(self.path.parent.glob('history.db.v6-backup-*'))
        with closing(sqlite3.connect(str(backup))) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(len(c.execute('SELECT * FROM core_samples').fetchone()), 13)

    def test_failed_v7_upgrade_rolls_back(self):
        self.old_database()
        with patch('hub.history.store.MIGRATION_7', 'ALTER TABLE core_samples ADD COLUMN psi_cpu_some_avg10 REAL; BAD SQL;'):
            with self.assertRaises(sqlite3.Error):
                HistoryStore(self.path).open()
        with closing(sqlite3.connect(str(self.path))) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(len(c.execute('SELECT * FROM core_samples').fetchone()), 13)
        store = HistoryStore(self.path).open()
        store.close()


if __name__ == '__main__':
    unittest.main()
