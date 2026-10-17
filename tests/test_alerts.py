import json
import tempfile
import unittest
from pathlib import Path
from hub.alerts import AlertEngine, validate_rules
from hub.history.events import query_events
from hub.history.store import HistoryStore
from test_backend import NODE_ID


class MemoryHistory:
    def __init__(self, restored=None):
        self.rows, self.restored, self.accept = [], restored or [], True
    def alert_state(self): return self.restored
    def submit(self, row):
        if self.accept: self.rows.append(row)
        return self.accept


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.rules = validate_rules({'rules':[dict(id='cpu', condition='cpu_percent', warning=80, critical=95,
            duration_seconds=10, recovery_seconds=5, cooldown_seconds=20, hysteresis=5)]})
        self.history = MemoryHistory()
        self.engine = AlertEngine(self.rules, self.history, clock=lambda:self.now, wall=lambda:1000+self.now)
        self.node = dict(node_id=NODE_ID, enabled=True, headline='live', data_current=True, overview={'cpu_percent':90})

    def tick(self, seconds, value=90, current=True):
        self.now = seconds
        self.node.update(overview={'cpu_percent':value}, data_current=current)
        self.engine.evaluate([self.node])
        return self.engine._states[(NODE_ID, 'cpu')]['state']

    def test_duration_hysteresis_recovery_and_cooldown(self):
        self.assertEqual(self.tick(0), 'Pending')
        self.assertEqual(self.tick(5, 70), 'Normal')
        self.assertEqual(self.tick(6), 'Pending')
        self.assertEqual(self.tick(16), 'Active')
        self.assertEqual(self.tick(17, 79), 'Active')
        self.assertEqual(self.tick(18, 70), 'Recovering')
        self.assertEqual(self.tick(22, 70), 'Recovering')
        self.assertEqual(self.tick(23, 70), 'Resolved')
        self.assertEqual(self.tick(42), 'Resolved')
        self.assertEqual(self.tick(43), 'Pending')

    def test_missing_stale_and_critical_escalation(self):
        self.tick(0)
        self.assertEqual(self.tick(20, current=False), 'Normal')
        self.tick(21)
        self.tick(31)
        self.tick(32, 99)
        self.assertEqual(self.engine.snapshot()['alerts'][0]['severity'], 'critical')
        self.assertEqual(self.tick(40, 0, current=False), 'Active')
        self.assertFalse(self.engine.snapshot()['alerts'][0]['known'])
        self.assertEqual(self.tick(41, 70), 'Recovering')
        self.assertEqual(self.tick(50, None), 'Active')
        self.assertEqual(self.tick(51, 70), 'Recovering')
        self.assertEqual(self.tick(56, 70), 'Resolved')

    def test_persist_restore_and_node_removal(self):
        self.tick(0)
        self.tick(10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'history.db'
            store = HistoryStore(path).open()
            try:
                store.insert_batch(self.history.rows)
                restored = store.connection.execute('SELECT node_id,rule_id,config_json,state_json FROM alert_states').fetchall()
                self.assertEqual(len(query_events(path, 'from_ms=0&to_ms=2000000&type=alert_transition')['events']), 2)
                history = MemoryHistory(restored)
                engine = AlertEngine(self.rules, history, clock=lambda:0)
                self.node['data_current'] = False
                engine.evaluate([self.node])
                self.assertEqual(engine.snapshot()['alerts'][0]['state'], 'Active')
                self.assertFalse(engine.snapshot()['alerts'][0]['known'])
                engine.evaluate([])
                store.insert_batch(history.rows)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM alert_states').fetchone()[0], 0)
            finally: store.close()

    def test_duplicate_suppression_and_queue_retry(self):
        self.history.accept = False
        self.tick(0)
        self.tick(1)
        self.assertEqual(self.engine.snapshot()['pending_persistence'], 1)
        self.history.accept = True
        self.tick(2)
        self.assertEqual(len(self.history.rows), 1)
        self.tick(3)
        self.assertEqual(len(self.history.rows), 1)

    def test_boolean_evidence_and_rules(self):
        for condition, headline in [('offline', 'offline'), ('stale', 'stale')]:
            rule = validate_rules({'rules':[dict(id=condition, condition=condition, duration_seconds=0)]})
            engine = AlertEngine(rule, self.history)
            self.node.update(headline=headline, data_current=False)
            engine.evaluate([self.node]); engine.evaluate([self.node])
            self.assertEqual(engine.snapshot()['alerts'][0]['state'], 'Active')
            self.node['headline'] = 'unknown'
            engine.evaluate([self.node])
            self.assertEqual(engine.snapshot()['alerts'][0]['state'], 'Active')
        self.assertIsNone(AlertEngine._value(self.node, 'optional_error'))
        self.node['optional_alert_error'] = True
        self.assertTrue(AlertEngine._value(self.node, 'optional_error'))
        for source in ({'rules':[{'id':'x','condition':'cpu_percent','warning':float('nan'),'critical':99}]},
                       {'rules':[{'id':'x','condition':'offline','warning':3}]}, {'rules':[self.rules[0]]*33}):
            with self.assertRaises(ValueError): validate_rules(source)

    def test_npu_and_probe_failure_conditions(self):
        rules = validate_rules({'rules':[dict(id='npu', condition='npu_percent', warning=80, critical=95, duration_seconds=0),
            dict(id='probe', condition='probe_failure_percent', warning=20, critical=50, duration_seconds=0)]})
        engine = AlertEngine(rules, self.history)
        # Probe evidence has its own freshness: a stale core does not hide it.
        self.node.update(overview={'npu_percent': 90}, data_current=False, probe_failure_percent=60)
        engine.evaluate([self.node]); engine.evaluate([self.node])
        states = {row['rule_id']: row for row in engine.snapshot()['alerts']}
        self.assertNotIn('npu', states)
        self.assertEqual((states['probe']['state'], states['probe']['severity']), ('Active', 'critical'))
        self.node['probe_failure_percent'] = None
        engine.evaluate([self.node])
        self.assertEqual(engine._states[(NODE_ID, 'probe')]['state'], 'Active')
        self.node['data_current'] = True
        engine.evaluate([self.node]); engine.evaluate([self.node])
        self.assertEqual(engine._states[(NODE_ID, 'npu')]['state'], 'Active')
        with self.assertRaises(ValueError):
            validate_rules({'rules':[dict(id='p', condition='probe_failure_percent', warning=20, critical=120)]})

    def test_probe_failure_percent_projection(self):
        from hub.state import _probe_failure_percent
        def groups(state='ok', age=0, targets=None):
            return {'probe': {'state': state, 'stale_after_ms': 30000, 'observation': {'id': 1, 'data_age_ms': age},
                              'data': {'targets': targets if targets is not None else
                                       [{'failure_ratio': 0.1}, {'failure_ratio': 0.3}, {'failure_ratio': None}]}}}
        self.assertEqual(_probe_failure_percent(groups(), 0), 30.0)
        self.assertEqual(_probe_failure_percent(groups('error'), 0), 30.0)
        self.assertIsNone(_probe_failure_percent(groups(), 30001))
        self.assertIsNone(_probe_failure_percent(groups(age=20000), 10001))
        self.assertIsNone(_probe_failure_percent(groups('starting'), 0))
        self.assertIsNone(_probe_failure_percent(groups(targets=[]), 0))
        self.assertIsNone(_probe_failure_percent(groups(targets=[{'failure_ratio': 1.5}]), 0))
        self.assertIsNone(_probe_failure_percent({}, 0))

    def test_writer_restores_state_and_api_reports_it(self):
        import threading
        import urllib.request
        from hub.history.writer import HistoryWriter
        from hub.runtime import Fleet
        from hub.http import HubServer
        from test_history import wait_for
        import time
        self.engine.wall = time.time
        self.tick(0); self.tick(10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'history.db'
            writer = HistoryWriter(path)
            writer.start()
            try:
                for row in self.history.rows: self.assertTrue(writer.submit(row))
                wait_for(lambda: writer.snapshot()['persisted'] >= 2)
            finally: writer.close()
            writer = HistoryWriter(path)
            fleet = Fleet([], history=writer, alert_rules=self.rules)
            server = HubServer(('127.0.0.1', 0), fleet)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            writer.start()
            try:
                wait_for(lambda: writer.alert_state() is not None)
                self.assertEqual(len(writer.alert_state()), 1)
                fleet.alerts.evaluate([self.node])
                with urllib.request.urlopen('http://127.0.0.1:%d/api/alerts' % server.server_port) as response:
                    document = json.load(response)
                self.assertEqual(document['schema_version'], 1)
                self.assertEqual(document['alerts'][0]['state'], 'Active')
            finally:
                server.shutdown(); server.server_close(); thread.join(); fleet.close()
