"""Bounded independent per-node polling workers."""

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import protocol
from .state import NodeState

LOG = logging.getLogger(__name__)


class Fleet:
    def __init__(self, nodes, *, client=None, core_interval=5.0, observations_interval=10.0,
                 status_deadline=2.0, stats_deadline=2.0, observations_deadline=3.0,
                 max_concurrency=16, clock=time.monotonic):
        if not (0 < core_interval and 0 < observations_interval and max_concurrency >= 1):
            raise ValueError("invalid polling configuration")
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

    def core_once(self, node_id):
        state = self.nodes[node_id]
        started = self.clock()
        try:
            raw = self.client.get(state.node.endpoint, "/api/status", self.deadlines[0])
            status = protocol.status(raw)
            state.note_valid_status(status)
            if status["ready"]:
                try:
                    stats = self.client.get(state.node.endpoint, "/api/stats", self.deadlines[1])
                    state.accept_core(status, stats)
                except protocol.AgentFailure as exc:
                    # Status succeeded; stats failure is a distinct transport or protocol event.
                    state.fail_core(exc)
        except protocol.AgentFailure as exc:
            state.fail_core(exc)
        except Exception:
            LOG.exception("unexpected core poll error node_id=%s", node_id)
            state.fail_core(protocol.ProtocolFailure("unexpected core poll error"))
        finally:
            state.record_poll_duration("core", max(0.0, (self.clock() - started) * 1000))

    def observations_once(self, node_id):
        state = self.nodes[node_id]
        if state.snapshot(include_details=False)["instance_id"] is None:
            return
        started = self.clock()
        try:
            raw = self.client.get(state.node.endpoint, "/api/observations", self.deadlines[2])
            state.accept_observations(raw)
        except protocol.AgentFailure as exc:
            state.fail_observations(exc)
        except Exception:
            LOG.exception("unexpected observations poll error node_id=%s", node_id)
            state.fail_observations(protocol.ProtocolFailure("unexpected optional poll error"))
        finally:
            state.record_poll_duration("observations", max(0.0, (self.clock() - started) * 1000))

    def _schedule(self):
        """One active request chain per node, globally bounded and fair."""
        ids = [node_id for node_id, state in self.nodes.items() if state.node.enabled]
        cursor = 0
        while not self._stop.is_set():
            now = self.clock()
            for node_id, (future, kind) in list(self._active.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception:
                        LOG.exception("poll worker unexpectedly raised node_id=%s", node_id)
                    del self._active[node_id]
                    period = self.core_interval if kind == "core" else self.observations_interval
                    details = self.nodes[node_id].snapshot(include_details=False)
                    if kind == "core" and details["core_error"] == "transport":
                        period = min(60.0, period * (2 ** min(details["consecutive_transport_failures"], 4)))
                    elif kind == "observations" and details["optional_error"] is not None:
                        period = min(60.0, period * 2)
                    jitter = random.uniform(0, min(0.5, period * 0.1))
                    self._due[node_id][kind] = self.clock() + period + jitter
            if ids:
                # Rotate starting point to avoid starving later nodes at capacity.
                for offset in range(len(ids)):
                    if len(self._active) >= self.max_concurrency:
                        break
                    node_id = ids[(cursor + offset) % len(ids)]
                    if node_id in self._active:
                        continue
                    due = self._due[node_id]
                    kind = min(due, key=due.get)
                    if due[kind] <= now:
                        fn = self.core_once if kind == "core" else self.observations_once
                        self._active[node_id] = (self._pool.submit(fn, node_id), kind)
                cursor = (cursor + 1) % len(ids)
            self._stop.wait(0.05)

    def start(self):
        if self._started:
            raise RuntimeError("Fleet already started")
        self._started = True
        self._scheduler = threading.Thread(target=self._schedule, name="hub-scheduler", daemon=True)
        self._scheduler.start()

    def close(self):
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.join()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def list_nodes(self):
        return [node.snapshot(include_details=False) for node in self.nodes.values()]

    def get_node(self, node_id):
        state = self.nodes.get(node_id)
        return state.snapshot() if state is not None else None
