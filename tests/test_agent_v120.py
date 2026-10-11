"""Synthetic fixtures pinned to platmon v1.2.0, never real device traffic.

Fields follow docs/api.md and docs/battery.md at upstream commit
26eae6e975ce184e713418dd2858b56e50d3ab90; values exercise null/zero/sign handling.
"""
import copy
import csv
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from hub import protocol
from hub.export import export_history
from hub.history.projector import project_observation
from hub.history.query import query_history
from hub.history.schema import SQL, MIGRATION_2, MIGRATION_3, MIGRATION_4, VERSION
from hub.history.store import HistoryStore
from hub.registry import Node
from hub.runtime import Fleet
from hub.state import NodeState
from test_backend import FakeClient, FakeClock, NODE_ID
from test_history import record


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures' / 'agent-v1.2.0.json').read_text(encoding='utf-8'))


class Agent120Tests(unittest.TestCase):
    def setUp(self):
        self.document = fixture()
        self.node = Node(NODE_ID, 'Synthetic 1.2.0 host', 'http://192.0.2.1:9797')
        self.clock = FakeClock()
        self.state = NodeState(self.node, clock=self.clock)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.db'

    def core(self):
        return self.state.accept_core(self.document['status'], self.document['stats'], received_at_ms=1000)

    def test_core_npu_null_zero_range_and_old_agents(self):
        accepted = self.core()
        self.assertTrue(accepted.record.valid())
        self.assertEqual(accepted.record.npu_percent, 0)
        self.assertEqual(self.state.snapshot()['overview']['gpu_percent'], 12.5)
        for value in (None, True, -1, 101, '12', float('inf')):
            self.document['stats']['npu']['usage'] = value
            self.core()
            self.assertIsNone(self.state.snapshot()['overview']['npu_percent'])
            if value is not None:
                self.assertFalse(replace(accepted.record, npu_percent=value).valid())
        del self.document['stats']['npu']
        self.document['stats']['collectors'].pop('npu')
        self.core()
        self.assertEqual(self.state.snapshot()['headline'], 'live')
        self.assertIsNone(self.state.snapshot()['overview']['npu_percent'])
        self.document['stats']['gpu'] = dict(driver='unknown', usage=None, freq=None, max_freq=None)
        self.core()
        self.assertIsNone(self.state.snapshot()['overview']['gpu_percent'])
        self.assertEqual(self.state.snapshot()['stats']['gpu']['driver'], 'unknown')

    def test_battery_freshness_is_independent_and_diagnostics_preserved(self):
        self.core()
        raw = self.document['observations']
        self.assertTrue(self.state.accept_observations(raw))
        self.clock.advance(20)
        self.assertEqual(self.state.snapshot()['headline'], 'stale')
        self.assertEqual(self.state.snapshot()['observations']['battery']['state'], 'ok')
        self.clock.advance(161)
        self.assertEqual(self.state.snapshot()['observations']['battery']['state'], 'stale')
        self.core()
        for state, expected in [('unavailable', None), ('partial', True), ('error', True), ('ok', False)]:
            raw['groups']['battery']['state'] = state
            self.state.accept_observations(copy.deepcopy(raw))
            self.assertEqual(self.state.snapshot()['optional_alert_error'], expected)
            self.assertEqual(self.state.snapshot()['headline'], 'live')

    def test_battery_projection_sign_units_null_and_absent_bay(self):
        group = self.document['observations']['groups']['battery']
        battery = group['data']['batteries'][0]
        battery.update(secret='do not persist', power_uw=-1200000)
        def project():
            return project_observation(NODE_ID, 'a', 'battery', group, 1000)
        result = project()
        self.assertTrue(result.valid())
        data = json.loads(result.data_json)
        self.assertEqual(data['batteries'][0]['charge_now_uah'], 3026000)
        self.assertIsNone(data['batteries'][0]['energy_now_uwh'])
        self.assertEqual(data['batteries'][0]['current_ua'], -250000)
        self.assertEqual(data['batteries'][0]['power_uw'], -1200000)
        self.assertIsNone(data['external'][0]['online'])
        self.assertNotIn('secret', result.data_json)
        battery.update(present=False, percent=80)
        absent = json.loads(project().data_json)['batteries'][0]
        self.assertFalse(absent['present'])
        self.assertTrue(all(value is None for key, value in absent.items() if key not in ('name', 'present')))
        battery.update(present=True, percent=101, voltage_uv=-1, current_ua=True)
        invalid = json.loads(project().data_json)['batteries'][0]
        for key in ('percent', 'voltage_uv', 'current_ua'):
            self.assertIsNone(invalid[key])
        battery['name'] = 'x' * 20000
        self.assertFalse(project().valid())
        group['state'] = 'stale'
        self.assertIsNone(project())

    def test_new_fields_do_not_relax_schema_gates(self):
        with self.assertRaises(protocol.ContractFailure):
            protocol.stats(dict(self.document['stats'], schema_version=2), 'agent-1.2.0-fixture')
        with self.assertRaises(protocol.ContractFailure):
            protocol.observations(dict(self.document['observations'], schema_version=3), 'agent-1.2.0-fixture')

    def test_poll_persistence_dedup_history_and_export(self):
        store = HistoryStore(self.path).open()
        self.addCleanup(store.close)
        class Sink:
            def submit(self, item):
                if item is not None:
                    self_outer.assertTrue(item.valid())
                    store.insert_batch([item])
            def close(self): pass
        self_outer = self
        client = FakeClient()
        client.handlers = {'/api/' + name: data for name, data in self.document.items()}
        fleet = Fleet([self.node], client=client, history=Sink(), clock=self.clock)
        self.addCleanup(fleet.close)
        fleet.core_once(NODE_ID)
        fleet.observations_once(NODE_ID)
        fleet.observations_once(NODE_ID)
        c = store.connection
        self.assertEqual(c.execute('SELECT count(*) FROM observations WHERE group_name="battery"').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT reason FROM continuity WHERE scope="battery"').fetchone(), (None,))
        when = c.execute('SELECT received_at_ms FROM core_samples').fetchone()[0]
        points = query_history(self.path, NODE_ID, 'npu_percent', when, when + 10000, 100)['points']
        self.assertEqual(points[0]['average'], 0)
        out = self.path.with_suffix('.zip')
        export_history(self.path, out, when, when + 10000, observations=True, reports=True)
        with zipfile.ZipFile(out) as z:
            def rows(name):
                return list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))
            core = rows(next(n for n in z.namelist() if n.startswith('measurements/')))
            self.assertEqual(core[0]['npu_percent'], '0.0')
            observed = rows('observations.csv')
            self.assertEqual(observed[0]['group_name'], 'battery')
            self.assertEqual(json.loads(observed[0]['data_json'])['batteries'][0]['current_ua'], -250000)
            summary = rows('summary-daily.csv')[0]
            self.assertEqual(summary['npu_percent_count'], '1')
            self.assertEqual(summary['npu_percent_average'], '0.0')
            self.assertEqual(summary['hottest_at_ms'], str(when))
        self.document['observations']['groups']['battery']['state'] = 'stale'
        self.document['observations']['groups']['battery']['observation']['id'] += 1
        fleet.observations_once(NODE_ID)
        self.assertEqual(c.execute('SELECT count(*) FROM observations').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT reason FROM continuity WHERE scope="battery"').fetchone(), ('stale',))

    def old_database(self):
        with closing(sqlite3.connect(str(self.path))) as c, c:
            c.executescript(SQL + MIGRATION_2 + MIGRATION_3 + MIGRATION_4)
            c.execute('INSERT INTO core_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', record().values()[:12])

    def test_v4_upgrade_preserves_rows_and_verified_backup(self):
        self.old_database()
        store = HistoryStore(self.path).open()
        try:
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], VERSION)
            self.assertEqual(store.connection.execute('SELECT cpu_percent,npu_percent FROM core_samples').fetchone(), (25, None))
            store.insert_batch([replace(record(2), npu_percent=35)])
        finally:
            store.close()
        backup = next(self.path.parent.glob('history.db.v4-backup-*'))
        with closing(sqlite3.connect(str(backup))) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(len(c.execute('SELECT * FROM core_samples').fetchone()), 12)

    def test_failed_v5_upgrade_rolls_back(self):
        self.old_database()
        with patch('hub.history.store.MIGRATION_5', 'ALTER TABLE core_samples ADD COLUMN npu_percent REAL; BAD SQL;'):
            with self.assertRaises(sqlite3.Error):
                HistoryStore(self.path).open()
        with closing(sqlite3.connect(str(self.path))) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(len(c.execute('SELECT * FROM core_samples').fetchone()), 12)
