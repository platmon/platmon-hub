"""Bounded independent per-node polling workers."""

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import protocol
from .state import NodeState, OPTIONAL_UNSUPPORTED
from .registry import validate_registry
from .reload import RegistryWatcher
from dataclasses import asdict
from .history.projector import ContinuityRecord, project_observation, EventRecord, OBSERVATION_GROUPS

LOG = logging.getLogger(__name__)


class Fleet:
    def __init__(self, nodes, *, client=None, core_interval=5.0, observations_interval=10.0,
                 status_deadline=2.0, stats_deadline=2.0, observations_deadline=3.0,
                 max_concurrency=16, clock=time.monotonic, history=None, registry_path=None, alert_rules=None):
        if not (0 < core_interval and 0 < observations_interval and max_concurrency >= 1):
            raise ValueError("invalid polling configuration")
        self._registry_lock = threading.RLock()
        self.watcher = RegistryWatcher(registry_path, self.apply_registry) if registry_path else None
        if alert_rules is not None and history is None:
            raise ValueError('alerts require history')
        self.history = history
        from .replay import ReplayCollector
        self.replay = ReplayCollector(history, clock) if history is not None and getattr(history, 'raw_enabled', False) else None
        from .alerts import AlertEngine
        self.alerts = AlertEngine(alert_rules, history, clock=clock) if alert_rules is not None else None
        self.client = client or protocol.HttpAgentClient()
        self.clock = clock
        self.core_interval = core_interval
        self.observations_interval = observations_interval
        self.deadlines = (status_deadline, stats_deadline, observations_deadline)
        if any(x <= 0 for x in self.deadlines):
            raise ValueError("invalid request deadline")
        self.nodes = {n.node_id: NodeState(n, clock=clock, core_interval=core_interval,
                                          status_deadline=status_deadline, stats_deadline=stats_deadline) for n in nodes}
        self.max_concurrency = max_concurrency
        self._pool = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="hub-poll")
        self._stop = threading.Event()
        self._scheduler = None
        self._active = {}  # node_id -> (future, endpoint_kind)
        self._due = {node_id: {"core": clock(), "observations": clock()}
                     for node_id in self.nodes}
        self._started = False

    def _history_submit(self, record):
        if self.history is not None and record is not None:
            try:
                self.history.submit(record)
            except Exception:
                LOG.exception("history submission failed node_id=%s", record.node_id)

    def _history_core_state(self, state):
        if self.history is None:
            return
        snapshot = state.snapshot(include_details=False)
        reason = None if snapshot['data_current'] else snapshot['headline']
        if reason == 'unknown':
            reason = 'transport' if snapshot['core_error'] == 'transport' else 'unobserved'
        elif snapshot['core_error'] == 'transport' and reason != 'offline':
            reason = 'transport'
        self._history_submit(ContinuityRecord(state.node.node_id, 'core', snapshot['instance_id'],
                                             int(time.time() * 1000), reason, state.grace_ms + 6000, snapshot['headline']))

    def apply_registry(self, nodes):
        nodes = validate_registry({'nodes': [asdict(node) for node in nodes]})
        with self._registry_lock:
            replacement = {}
            now = self.clock()
            for node in nodes:
                old = self.nodes.get(node.node_id)
                if old is None or old.node != node:
                    reason = 'added' if old is None else 'endpoint_changed' if old.node.endpoint != node.endpoint else 'enabled_changed' if old.node.enabled != node.enabled else 'renamed'
                    self._history_submit(EventRecord.create(node.node_id, 'registry_changed', int(time.time()*1000),
                                                           None if old is None else ('enabled' if old.node.enabled else 'disabled'),
                                                           'enabled' if node.enabled else 'disabled', reason))
                if old is not None and old.node.endpoint == node.endpoint and old.node.enabled == node.enabled:
                    old.update_node(node)
                    replacement[node.node_id] = old
                else:
                    replacement[node.node_id] = NodeState(node, clock=self.clock, core_interval=self.core_interval,
                                                         status_deadline=self.deadlines[0], stats_deadline=self.deadlines[1])
                    self._due[node.node_id] = {'core': now, 'observations': now}
                    instance = old.snapshot(include_details=False)['instance_id'] if old else None
                    self._history_submit(ContinuityRecord(node.node_id, 'core', instance, int(time.time()*1000),
                                                         'unobserved' if node.enabled else 'disabled'))
            for node_id in set(self.nodes) - set(replacement):
                self._history_submit(EventRecord.create(node_id, 'registry_changed', int(time.time()*1000), 'registered', 'removed', 'removed'))
                self._due.pop(node_id, None)
            self.nodes = replacement
            if self.replay is not None:
                self.replay.reconcile(replacement)

    def _current(self, node_id, state):
        return self.nodes.get(node_id) is state and state.node.enabled

    def core_once(self, node_id, state=None):
        with self._registry_lock:
            state = state or self.nodes.get(node_id)
            if state is None or not self._current(node_id, state):
                return
            endpoint = state.node.endpoint
        started = self.clock()
        try:
            status = protocol.status(self.client.get(endpoint, '/api/status', self.deadlines[0]))
            with self._registry_lock:
                if not self._current(node_id, state): return
                state.note_valid_status(status)
            if status['ready']:
                stats = self.client.get(endpoint, '/api/stats', self.deadlines[1])
                received = int(time.time()*1000)
                with self._registry_lock:
                    if not self._current(node_id, state): return
                    accepted = state.accept_core(status, stats, received_at_ms=received)
                    self._history_submit(accepted.record)
        except protocol.AgentFailure as exc:
            with self._registry_lock:
                if self._current(node_id, state): state.fail_core(exc)
        except Exception:
            LOG.exception('unexpected core poll error node_id=%s', node_id)
            with self._registry_lock:
                if self._current(node_id, state): state.fail_core(protocol.ProtocolFailure('unexpected core poll error'))
        finally:
            with self._registry_lock:
                if self._current(node_id, state):
                    state.record_poll_duration('core', max(0.0, (self.clock()-started)*1000))
                    self._history_core_state(state)

    def observations_once(self, node_id, state=None):
        with self._registry_lock:
            state = state or self.nodes.get(node_id)
            if state is None or not self._current(node_id, state) or state.snapshot(include_details=False)['instance_id'] is None:
                return
            endpoint = state.node.endpoint
        started = self.clock()
        try:
            raw = self.client.get(endpoint, '/api/observations', self.deadlines[2])
            received = int(time.time()*1000)
            with self._registry_lock:
                if not self._current(node_id, state): return
                if state.accept_observations(raw) and self.history is not None:
                    for name in OBSERVATION_GROUPS:
                        group = raw['groups'].get(name)
                        reason, threshold = 'absent', 30000
                        if group is not None:
                            threshold = max(1, group['stale_after_ms'])
                            publication = group.get('observation')
                            reason = group['state'] if group['state'] != 'ok' else None
                            if publication and (publication.get('stale') is True or publication['data_age_ms'] > threshold):
                                reason = 'stale'
                            if reason != 'stale':
                                self._history_submit(project_observation(node_id, raw['instance_id'], name, group, received))
                        self._history_submit(ContinuityRecord(node_id, name, raw['instance_id'], received, reason, threshold+13000))
        except protocol.AgentFailure as exc:
            with self._registry_lock:
                if self._current(node_id, state):
                    state.fail_observations(exc)
                    reason = 'unavailable' if exc.category in OPTIONAL_UNSUPPORTED else 'error'
                    for name in OBSERVATION_GROUPS:
                        self._history_submit(ContinuityRecord(node_id, name, state.snapshot(include_details=False)['instance_id'],
                                                             int(time.time()*1000), reason))
        except Exception:
            LOG.exception('unexpected observations poll error node_id=%s', node_id)
            with self._registry_lock:
                if self._current(node_id, state): state.fail_observations(protocol.ProtocolFailure('unexpected optional poll error'))
        finally:
            with self._registry_lock:
                if self._current(node_id, state): state.record_poll_duration('observations', max(0.0, (self.clock()-started)*1000))

    def replay_once(self, node_id, state, stream, cursor):
        from .replay import fetch_page
        with self._registry_lock:
            if not self._current(node_id, state):
                return
            endpoint = state.node.endpoint
        try:
            page, more = fetch_page(self.client, endpoint, node_id, stream, cursor, self.deadlines[0], self.deadlines[2])
            with self._registry_lock:
                if self._current(node_id, state):
                    self.replay.accept(node_id, state, stream, page, more)
        except Exception as exc:
            with self._registry_lock:
                if self._current(node_id, state):
                    self.replay.fail(node_id, state, stream, exc)

    def _schedule(self):
        """One active request chain per node, globally bounded and fair."""
        cursor = 0
        while not self._stop.is_set():
            now = self.clock()
            with self._registry_lock:
                for node_id, (future, kind, state) in list(self._active.items()):
                    if future.done():
                        try:
                            future.result()
                        except Exception:
                            LOG.exception('poll worker unexpectedly raised node_id=%s', node_id)
                        del self._active[node_id]
                        if not self._current(node_id, state):
                            continue
                        if kind == 'replay':
                            continue
                        period = self.core_interval if kind == 'core' else self.observations_interval
                        details = state.snapshot(include_details=False)
                        if kind == 'core' and details['core_error'] == 'transport':
                            period = min(60.0, period * (2 ** min(details['consecutive_transport_failures'], 4)))
                        elif kind == 'observations' and details['optional_error'] is not None:
                            period = min(60.0, period * 2)
                        self._due[node_id][kind] = self.clock()+period+random.uniform(0, min(0.5,period*0.1))
                ids = [key for key, state in self.nodes.items() if state.node.enabled]
                if ids:
                    for offset in range(len(ids)):
                        if len(self._active) >= self.max_concurrency: break
                        node_id = ids[(cursor+offset) % len(ids)]
                        if node_id in self._active: continue
                        due = self._due[node_id]
                        kind = min(due, key=due.get)
                        if due[kind] <= now:
                            fn = self.core_once if kind == 'core' else self.observations_once
                            state = self.nodes[node_id]
                            self._active[node_id] = (self._pool.submit(fn, node_id, state), kind, state)
                    # Only spare slots after all currently due live work is admitted.
                    if self.replay is not None:
                        for offset in range(len(ids)):
                            if len(self._active) >= self.max_concurrency: break
                            node_id = ids[(cursor+offset) % len(ids)]
                            if node_id in self._active: continue
                            state = self.nodes[node_id]
                            job = self.replay.ready(node_id, state)
                            if job is not None:
                                self._active[node_id] = (self._pool.submit(self.replay_once, node_id, state, *job), 'replay', state)
                    cursor = (cursor+1) % len(ids)
            self._stop.wait(0.05)

    def start(self):
        if self._started:
            raise RuntimeError("Fleet already started")
        self._started = True
        if self.history is not None:
            self.history.start()
        self._scheduler = threading.Thread(target=self._schedule, name="hub-scheduler", daemon=True)
        self._scheduler.start()
        if self.watcher is not None:
            self.watcher.start()
        if self.alerts is not None:
            self.alerts.start(self.list_nodes)

    def close(self):
        self._stop.set()
        if self.alerts is not None:
            self.alerts.close()
        if self.watcher is not None:
            self.watcher.close()
        if self._scheduler is not None:
            self._scheduler.join()
        self._pool.shutdown(wait=True, cancel_futures=True)
        if self.history is not None:
            self.history.close()

    def list_nodes(self):
        with self._registry_lock:
            return [node.snapshot(include_details=False) for node in self.nodes.values()]

    def get_node(self, node_id):
        with self._registry_lock:
            state = self.nodes.get(node_id)
            return state.snapshot() if state is not None else None
