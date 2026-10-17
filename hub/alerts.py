"""Local alert rules and monotonic state transitions; no external delivery."""
import json
import math
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .history.projector import PRESSURE_COLUMNS, EventRecord

# A low reading is the bad one for these: warning at or above critical, compared at or below, in (floor, ceiling).
LOW_METRICS = {'battery_percent': (0, 100), 'wifi_signal_dbm': (-120, 0)}
# Evidence from an optional observation group, which has its own freshness, not core's.
OBSERVED = ('probe_failure_percent',) + tuple(LOW_METRICS)
METRICS = ('cpu_percent', 'memory_percent', 'disk_percent', 'hottest_c', 'gpu_percent', 'npu_percent',
           'probe_failure_percent') + tuple(LOW_METRICS) + PRESSURE_COLUMNS
CONDITIONS = METRICS + ('offline', 'stale', 'optional_error')
STATES = ('Normal', 'Pending', 'Active', 'Recovering', 'Resolved')


def load_rules(path):
    with Path(path).open(encoding='utf-8') as stream:
        raw = stream.read(65537)
    if len(raw.encode('utf-8')) > 65536:
        raise ValueError('alert configuration exceeds 64 KiB')
    return validate_rules(json.loads(raw))


def validate_rules(document):
    if not isinstance(document, dict) or set(document) != {'rules'} or not isinstance(document['rules'], list) or len(document['rules']) > 32:
        raise ValueError('expected at most 32 alert rules')
    result, identities = [], set()
    for source in document['rules']:
        allowed = {'id', 'condition', 'warning', 'critical', 'hysteresis', 'duration_seconds', 'recovery_seconds', 'cooldown_seconds'}
        if not isinstance(source, dict) or set(source)-allowed:
            raise ValueError('invalid alert rule fields')
        identity, condition = source.get('id'), source.get('condition')
        if not isinstance(identity, str) or not re.fullmatch('[a-z0-9_-]{1,48}', identity) or identity in identities or condition not in CONDITIONS:
            raise ValueError('invalid or duplicate alert identity/condition')
        identities.add(identity)
        rule = dict(source)
        for key, default in (('duration_seconds', 30), ('recovery_seconds', 30), ('cooldown_seconds', 300), ('hysteresis', 5)):
            rule.setdefault(key, default)
        for key in ('duration_seconds', 'recovery_seconds', 'cooldown_seconds', 'hysteresis'):
            value = rule[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400:
                raise ValueError('invalid alert duration or hysteresis')
        if condition in LOW_METRICS:
            floor, ceiling = LOW_METRICS[condition]
            for key in ('warning', 'critical'):
                value = rule.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or not floor <= value <= ceiling:
                    raise ValueError('low alert thresholds must be finite and within %g..%g' % (floor, ceiling))
            # Recovery needs a reading above warning + hysteresis, so that must be reachable.
            if rule['critical'] > rule['warning'] or rule['warning'] + rule['hysteresis'] > ceiling:
                raise ValueError('invalid alert threshold ordering')
        elif condition in METRICS:
            for key in ('warning', 'critical'):
                value = rule.get(key)
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError('numeric alerts require finite warning and critical thresholds')
                if condition != 'hottest_c' and not 0 <= value <= 100:
                    raise ValueError('percentage threshold outside 0..100')
            if rule['critical'] < rule['warning'] or rule['hysteresis'] > rule['warning'] and condition != 'hottest_c':
                raise ValueError('invalid alert threshold ordering')
        elif any(key in source for key in ('warning', 'critical', 'hysteresis')):
            raise ValueError('boolean conditions do not accept thresholds')
        result.append(rule)
    return result



@dataclass(frozen=True)
class AlertRecord:
    node_id: str
    rule_id: str
    received_at_ms: int
    config_json: str
    state_json: str
    event: object
    delete: bool = False

    def valid(self):
        return (self.event.valid() and self.event.node_id == self.node_id and
                len(self.config_json.encode('utf-8')) <= 4096 and len(self.state_json.encode('utf-8')) <= 4096)


class AlertEngine:
    def __init__(self, rules, history, *, clock=time.monotonic, wall=time.time):
        self.rules, self.history = rules, history
        self.clock, self.wall = clock, wall
        self._lock = threading.Lock()
        self._states = {}
        self._ready = False
        self._stop = threading.Event()
        self._thread = None
        self._pending = {}
        self._lost_transitions = 0
        self._error = None

    def _save(self, key, state, rule, previous, reason, delete=False):
        public = {k: v for k, v in state.items() if not k.startswith('_')}
        event = EventRecord.create(key[0], 'alert_transition', int(self.wall()*1000), previous, state['state'], rule['id']+':'+reason)
        record = AlertRecord(key[0], key[1], event.received_at_ms, json.dumps(rule, sort_keys=True), json.dumps(public), event, delete)
        # One latest unsent state per rule/node. Transition loss remains visible in writer counters.
        if key in self._pending:
            self._lost_transitions += 1
        if key in self._pending or len(self._pending) < 8192:
            self._pending[key] = record
        else:
            self._lost_transitions += 1

    def evaluate(self, nodes):
        with self._lock:
            now = self.clock()
            if not self._ready:
                restored = self.history.alert_state()
                if restored is None:
                    return
                by_id = {rule['id']: rule for rule in self.rules}
                for node, rule_id, config, saved in restored:
                    rule = by_id.get(rule_id)
                    state = json.loads(saved)
                    if rule is not None and json.loads(config) == rule:
                        state.update(_since=now, _cooldown=now+rule['cooldown_seconds'] if state['state'] == 'Resolved' else 0, known=False)
                        state['state'] = 'Active' if state['state'] in ('Active', 'Recovering') else 'Resolved' if state['state'] == 'Resolved' else 'Normal'
                        self._states[(node, rule_id)] = state
                    else:
                        old_rule = json.loads(config)
                        state['state'] = 'Resolved'
                        self._save((node, rule_id), state, old_rule, 'configuration', 'rule_changed', delete=True)
                self._ready = True
            current_keys = set()
            for node in nodes:
                if not node['enabled']:
                    continue
                for rule in self.rules:
                    key = (node['node_id'], rule['id'])
                    current_keys.add(key)
                    state = self._states.setdefault(key, dict(node_id=key[0], rule_id=key[1], condition=rule['condition'],
                        state='Normal', severity=None, known=False, value=None, changed_at_ms=int(self.wall()*1000), _since=now, _cooldown=0))
                    previous, severity = state['state'], state['severity']
                    value = self._value(node, rule['condition'])
                    state.update(known=value is not None, value=value)
                    if value is None:
                        if previous == 'Pending': state['state'] = 'Normal'
                        if previous == 'Recovering': state['state'] = 'Active'
                        state['_since'] = now
                    else:
                        numeric, low = rule['condition'] in METRICS, rule['condition'] in LOW_METRICS
                        if low:
                            high = value <= rule['warning']
                            recovered = value > rule['warning']+rule['hysteresis']
                            level = 'critical' if value <= rule['critical'] else 'warning'
                        else:
                            high = value >= rule['warning'] if numeric else value
                            recovered = value < rule['warning']-rule['hysteresis'] if numeric else not value
                            level = ('critical' if value >= rule['critical'] else 'warning') if numeric else 'critical'
                        if previous in ('Normal', 'Resolved'):
                            if high and now >= state['_cooldown']:
                                state.update(state='Pending', severity=level, _since=now)
                        elif previous == 'Pending':
                            if not high: state.update(state='Normal', severity=None, _since=now)
                            elif now-state['_since'] >= rule['duration_seconds']: state.update(state='Active', severity=level)
                        elif previous == 'Active':
                            if recovered: state.update(state='Recovering', _since=now)
                            elif high and level == 'critical': state['severity'] = level
                        elif previous == 'Recovering':
                            if not recovered: state.update(state='Active', _since=now)
                            elif now-state['_since'] >= rule['recovery_seconds']:
                                state.update(state='Resolved', severity=None, _cooldown=now+rule['cooldown_seconds'])
                    if state['state'] != previous or state['severity'] != severity:
                        state['changed_at_ms'] = int(self.wall()*1000)
                        self._save(key, state, rule, previous, 'condition' if value is not None else 'unobserved')
            by_id = {rule['id']: rule for rule in self.rules}
            for key in set(self._states)-current_keys:
                state = self._states.pop(key)
                previous = state['state']
                state.update(state='Resolved', known=False)
                self._save(key, state, by_id[key[1]], previous, 'node_inactive', delete=True)
            for key, record in list(self._pending.items()):
                if self.history.submit(record):
                    del self._pending[key]

    @staticmethod
    def _value(node, condition):
        if condition in OBSERVED:
            # Optional groups have their own freshness; core state does not gate them.
            return node.get(condition) if node['enabled'] else None
        if condition in PRESSURE_COLUMNS:
            return node['pressure'].get(condition) if node['data_current'] else None
        if condition in METRICS:
            return node['overview'].get(condition) if node['data_current'] else None
        if condition == 'offline':
            return True if node['headline'] == 'offline' else False if node['data_current'] else None
        if condition == 'stale':
            return True if node['headline'] == 'stale' else False if node['data_current'] else None
        return node.get('optional_alert_error')

    def snapshot(self):
        with self._lock:
            rows = [{k: v for k, v in state.items() if not k.startswith('_')} for state in self._states.values() if state['state'] != 'Normal']
            rows.sort(key=lambda row: (row['state'] not in ('Active', 'Recovering'), -row['changed_at_ms'], row['node_id'], row['rule_id']))
            return dict(enabled=True, ready=self._ready, alerts=rows[:500], total=len(rows), pending_persistence=len(self._pending), lost_transitions=self._lost_transitions, error=self._error)

    def start(self, get_nodes):
        def run():
            while not self._stop.is_set():
                try:
                    self.evaluate(get_nodes())
                    self._error = None
                except Exception as exc:
                    if self._error != type(exc).__name__:
                        logging.getLogger(__name__).error('alert evaluation failed: %s', type(exc).__name__)
                    self._error = type(exc).__name__
                self._stop.wait(1)
        self._thread = threading.Thread(target=run, name='hub-alerts', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None: self._thread.join(timeout=2)
