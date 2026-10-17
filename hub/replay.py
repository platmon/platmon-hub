"""Fair one-page replay turns, isolated from NodeState health and alerts."""

import threading
import time

from . import protocol
from .history.raw import STREAMS, validate_page


def capture_level(token):
    """The node's raw capture level; tokens without a registry node use the default."""
    return getattr(getattr(token, 'node', None), 'capture', 'auto')


class ReplayCollector:
    def __init__(self, writer, clock=time.monotonic):
        self.writer, self.clock = writer, clock
        self._lock = threading.RLock()
        self._nodes = {}
        self._reservations = {}

    def reconcile(self, nodes):
        with self._lock:
            self._nodes = {key: value for key, value in self._nodes.items()
                           if key in nodes and nodes[key] is value['token']}
            self._reservations = {key: token for key, token in self._reservations.items()
                                  if key[0] in self._nodes and self._nodes[key[0]]['token'] is token}

    def _node(self, node_id, token):
        node = self._nodes.get(node_id)
        if node is None or node['token'] is not token:
            self._reservations = {key: value for key, value in self._reservations.items() if key[0] != node_id}
            node = dict(token=token, turn=0, streams={name: dict(
                due=0, future=None, phase=None, loaded=False, cursor=None,
                state='starting', error=None, failures=0, after_id=None,
                instance_id=None, has_more=False, pages=0, losses=0, supported=None) for name in STREAMS})
            self._nodes[node_id] = node
        return node

    def _failure(self, item, code, stopped=False, unavailable=False):
        item.update(future=None, loaded=False, state='stopped' if stopped else 'unavailable' if unavailable else 'retrying', error=code)
        if unavailable:
            item['supported'] = False
        item['failures'] = min(6, item['failures']+1)
        item['due'] = float('inf') if stopped else self.clock() + (60 if unavailable else min(60, 2**item['failures']))

    def ready(self, node_id, token):
        """Called by scheduler; asynchronous cursor reads and commits consume no workers."""
        with self._lock:
            node = self._node(node_id, token)
            if capture_level(token) not in ('auto', 'replay'):
                return None
            for offset in range(len(STREAMS)):
                index = (node['turn']+offset) % len(STREAMS)
                name = STREAMS[index]
                item = node['streams'][name]
                future = item['future']
                if future is not None:
                    if not future.done():
                        continue
                    try:
                        result = future.result()
                        if item['phase'] == 'cursor':
                            item.update(cursor=result, loaded=True, future=None,
                                        instance_id=result[0] if result else None, after_id=result[1] if result else None)
                        else:
                            # The commit result is the durable cursor; only failures re-read it.
                            cursor = (item['pending_instance_id'], result['after_id'])
                            item.update(future=None, loaded=True, cursor=cursor, state='captured', error=None, failures=0, supported=True,
                                        after_id=cursor[1], instance_id=cursor[0], pages=item['pages']+1,
                                        due=self.clock() + (0.05 if item['has_more'] else 5 if name == 'core' else 10))
                    except Exception as exc:
                        code = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
                        self._failure(item, code)
                if item['due'] > self.clock() or item['future'] is not None:
                    continue
                if not item['loaded']:
                    capacity = self.writer.raw_capacity()
                    if (capacity['queue_depth']+len(self._reservations) >= capacity['queue_capacity'] or
                            capacity['queued_bytes']+256+len(self._reservations)*3*1024**2 > capacity['queue_byte_limit']):
                        return None
                    item.update(future=self.writer.replay_cursor(node_id, name), phase='cursor')
                    return None
                capacity = self.writer.raw_capacity()
                if (capacity['queued_bytes'] + (len(self._reservations)+1)*3*1024**2 > capacity['queue_byte_limit'] or
                        capacity['queue_depth']+len(self._reservations) >= capacity['queue_capacity']):
                    return None
                node['turn'] = (index+1) % len(STREAMS)
                self._reservations[node_id, name] = token
                item['state'] = 'fetching'
                return name, item['cursor']
        return None

    def accept(self, node_id, token, stream, page, has_more):
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None or node['token'] is not token:
                return
            self._reservations.pop((node_id, stream), None)
            item = node['streams'][stream]
            item.update(future=self.writer.submit_raw(page), phase='commit', loaded=False,
                        has_more=has_more, pending_instance_id=page.instance_id, state='committing',
                        losses=item['losses']+len(page.losses))

    def fail(self, node_id, token, stream, exc):
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None or node['token'] is not token:
                return
            self._reservations.pop((node_id, stream), None)
            stopped = isinstance(exc, protocol.ContractFailure) or (isinstance(exc, protocol.ReplayConflict) and exc.code == 'cursor_ahead')
            code = exc.code if isinstance(exc, protocol.ReplayConflict) else exc.category if isinstance(exc, protocol.AgentFailure) else type(exc).__name__
            self._failure(node['streams'][stream], code, stopped, isinstance(exc, protocol.OptionalUnsupported))

    def sampled_streams(self, node_id, token, streams):
        """Streams whose polled documents are stored as sampled capture now (ADR-0007)."""
        level = capture_level(token)
        if level == 'sampled':
            return tuple(streams)
        if level != 'auto':
            return ()
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None or node['token'] is not token:
                return ()
            return tuple(name for name in streams if node['streams'][name]['supported'] is False)

    def _source(self, node, name):
        level = capture_level(node['token'])
        if level in ('sampled', 'off', 'replay'):
            return level
        return 'sampled' if node['streams'][name]['supported'] is False else 'replay'

    def snapshot(self):
        with self._lock:
            return {'enabled': True, 'nodes': {node_id: {name: dict({k: value for k, value in item.items()
                    if k in ('state', 'error', 'after_id', 'instance_id', 'has_more', 'pages', 'losses')},
                    source=self._source(node, name))
                    for name, item in node['streams'].items()} for node_id, node in self._nodes.items()},
                    'capture': {node_id: capture_level(node['token']) for node_id, node in self._nodes.items()}}


def fetch_page(client, endpoint, node_id, stream, cursor, status_deadline=2, replay_deadline=3):
    # Confirm identity independently each turn, including after 409 restart races.
    current = protocol.status(client.get(endpoint, '/api/status', status_deadline))['instance_id']
    previous, previous_after = cursor if cursor is not None else (None, 0)
    after = previous_after if previous == current else 0
    document = client.get_replay(endpoint, stream, current, after, replay_deadline)
    return validate_page(document, node_id, current, stream, after, int(time.time()*1000),
                         previous if previous != current else None, previous_after), document['has_more']
