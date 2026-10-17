"""Bounded, nonblocking ingress and independently retrying SQLite writer."""

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from .store import HistoryStore, SchemaError
from .schema import VERSION
from .projector import ContinuityRecord, EventRecord

LOG = logging.getLogger(__name__)


class HistoryWriter:
    def __init__(self, path, *, capacity=2048, retention_days=7, store_factory=HistoryStore,
                 retry_seconds=5.0, clock=time.monotonic, wall=time.time, event_retention_days=30,
                 raw_retention_days=None, raw_quota_bytes=None, raw_queue_bytes=8*1024**2):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('invalid history queue capacity')
        if not math.isfinite(retention_days) or retention_days <= 0:
            raise ValueError('invalid history retention')
        if not math.isfinite(event_retention_days) or event_retention_days <= 0:
            raise ValueError('invalid event retention')
        self.event_retention_ms = event_retention_days * 86400000
        self.raw_enabled = raw_retention_days is not None
        if self.raw_enabled and (not math.isfinite(raw_retention_days) or not 0 < raw_retention_days <= 365 or
                                 type(raw_quota_bytes) is not int or raw_quota_bytes < 1024**2):
            raise ValueError('raw capture requires explicit finite retention and quota')
        if type(raw_queue_bytes) is not int or not 3*1024**2 <= raw_queue_bytes <= 64*1024**2:
            raise ValueError('invalid raw queue byte limit')
        self.raw_retention_ms = raw_retention_days * 86400000 if self.raw_enabled else None
        self.raw_quota_bytes = raw_quota_bytes
        self.raw_queue_bytes = raw_queue_bytes
        self._raw_queue = queue.Queue(16)
        self._raw_bytes = 0
        self._raw_in_flight = 0
        self._raw_stats = dict(committed_pages=0, persisted=0, duplicates=0, rejected=0,
                               failed_pages=0, cursor_read_failures=0, last_error=None, retention_deleted=0,
                               quota_evicted=0)
        self.path = Path(path).resolve()
        self.retention_ms = retention_days * 86400000
        self.store_factory = store_factory
        self.retry_seconds = retry_seconds
        self.clock, self.wall = clock, wall
        self.queue = queue.Queue(capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._accepting = True
        self._losses = {}
        self._pending_events = {}
        self._alert_state = None
        self._last_overflow_event = float("-inf")
        self._stats = dict(enabled=True, state='starting', schema_version=None, submitted=0,
                           persisted=0, duplicates=0, dropped_regression=0, dropped_queue_full=0,
                           dropped_storage_error=0, dropped_invalid=0, dropped_closed=0,
                           dropped_expired=0, in_flight=0, last_commit_ms=None, last_error=None,
                           last_error_ms=None, retention_deleted=0, last_retention_ms=None,
                           clock_discontinuity=False, last_checkpoint_ms=None,
                           checkpoint_duration_ms=None, wal_bytes=None)

    def _update(self, **values):
        with self._lock:
            self._stats.update(values)

    def _count(self, **values):
        with self._lock:
            for key, value in values.items():
                self._stats[key] = self._stats.get(key, 0) + value

    def submit(self, record):
        with self._lock:
            if not self._accepting:
                self._stats['dropped_closed'] += 1
                return False
            if not record.valid():
                self._stats['dropped_invalid'] += 1
                return False
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                self._stats['dropped_queue_full'] += 1
                self._remember_loss(record)
                if self.clock() - self._last_overflow_event >= 60:
                    self._pending_events['history_queue_overflow'] = EventRecord.create(None, 'history_queue_overflow', int(self.wall()*1000), None, 'dropping', 'queue_full')
                    self._last_overflow_event = self.clock()
                return False
            self._stats['submitted'] += 1
            return True

    def _remember_loss(self, record):
        # Caller holds _lock. One earliest loss per node/scope, capped independently.
        if record.node_id is None:
            return
        key = (record.node_id, getattr(record, 'group_name', getattr(record, 'scope', 'core')))
        if key not in self._losses and len(self._losses) < 1024:
            self._losses[key] = ContinuityRecord(key[0], key[1], getattr(record, 'instance_id', None),
                                                 record.received_at_ms, 'storage_loss')

    def snapshot(self):
        with self._lock:
            return dict(self._stats, queue_depth=self.queue.qsize(), queue_capacity=self.queue.maxsize,
                        pending_events=len(self._pending_events), raw=dict(self._raw_stats,
                        enabled=self.raw_enabled, queue_depth=self._raw_queue.qsize(), queue_capacity=self._raw_queue.maxsize, in_flight=self._raw_in_flight,
                        queued_bytes=self._raw_bytes, queue_byte_limit=self.raw_queue_bytes,
                        quota_bytes=self.raw_quota_bytes, retention_ms=self.raw_retention_ms))

    def _raw_submit(self, operation, value, size):
        future = Future()
        with self._lock:
            if (not self.raw_enabled or not self._accepting or self._raw_queue.full() or
                    self._raw_bytes + size > self.raw_queue_bytes):
                self._raw_stats['rejected'] += 1
                future.set_exception(RuntimeError('raw_queue_unavailable'))
            else:
                self._raw_bytes += size
                self._raw_queue.put_nowait((operation, value, size, future))
        return future

    def replay_cursor(self, node_id, stream):
        return self._raw_submit('cursor', (node_id, stream), 256)

    def submit_raw(self, page):
        return self._raw_submit('page', page, page.encoded_bytes)

    def _raw_once(self, store):
        try:
            operation, value, size, future = self._raw_queue.get_nowait()
        except queue.Empty:
            return
        with self._lock:
            self._raw_in_flight = 1
        notify = future.set_running_or_notify_cancel()
        try:
            if store is None:
                raise RuntimeError('raw_storage_unavailable')
            if operation == 'cursor':
                result = store.replay_cursor(*value)
            else:
                result = store.insert_raw_page(value, self.raw_quota_bytes)
                with self._lock:
                    self._raw_stats['committed_pages'] += 1
                    self._raw_stats['persisted'] += result['inserted']
                    self._raw_stats['duplicates'] += result['duplicates']
                    self._raw_stats['quota_evicted'] += result['evicted']
                    self._raw_stats['last_error'] = None
            if notify:
                future.set_result(result)
        except Exception as exc:
            with self._lock:
                self._raw_stats['failed_pages' if operation == 'page' else 'cursor_read_failures'] += 1
                self._raw_stats['last_error'] = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            if notify:
                future.set_exception(exc)
        finally:
            with self._lock:
                self._raw_bytes -= size
                self._raw_in_flight = 0
        return operation == 'cursor'

    def alert_state(self):
        with self._lock:
            return None if self._alert_state is None else list(self._alert_state)

    def start(self):
        if self._thread is not None:
            raise RuntimeError('history writer already started')
        self._thread = threading.Thread(target=self._run, name='hub-history', daemon=True)
        self._thread.start()

    def _error(self, exc):
        category = str(exc) if isinstance(exc, SchemaError) else type(exc).__name__
        previous = self.snapshot()['last_error']
        self._update(state='degraded', last_error=category, last_error_ms=int(self.wall() * 1000))
        if category != previous:
            with self._lock:
                self._pending_events['history_writer_error'] = EventRecord.create(None, 'history_writer_error', int(self.wall()*1000), 'healthy', 'degraded', category)
            LOG.warning('history unavailable: %s', category)

    def _run(self):
        store = None
        batch = []
        retry_start = None
        next_open = 0
        fatal = False
        next_retention = self.clock()
        next_raw_retention = self.clock()
        last_wall, last_tick = self.wall(), self.clock()
        try:
            while not self._stop.is_set() or batch or not self.queue.empty() or self._pending_events or not self._raw_queue.empty():
                now = self.clock()
                wall = self.wall()
                if abs((wall - last_wall) - (now - last_tick)) > 60:
                    self._update(clock_discontinuity=True)
                last_wall, last_tick = wall, now
                if store is None and not fatal and now >= next_open:
                    candidate = self.store_factory(self.path)
                    try:
                        store = candidate.open()
                        restored = store.connection.execute('SELECT node_id,rule_id,config_json,state_json FROM alert_states LIMIT 8193').fetchall()
                        if len(restored) > 8192:
                            self._update(alert_restore_error='alert_state_limit_exceeded')
                        with self._lock:
                            self._alert_state = restored if len(restored) <= 8192 else None
                        if self.snapshot()['last_error'] is not None:
                            with self._lock:
                                self._pending_events['history_writer_recovered'] = EventRecord.create(None, 'history_writer_recovered', int(self.wall()*1000), 'degraded', 'healthy', 'opened')
                        self._update(state='healthy', schema_version=VERSION)
                    except Exception as exc:
                        candidate.close()
                        store = None
                        self._error(exc)
                        fatal = isinstance(exc, SchemaError)
                        next_open = now + 5
                if not batch:
                    try:
                        batch.append(self.queue.get(timeout=0 if not self._raw_queue.empty() else 0.05))
                    except queue.Empty:
                        pass
                    while len(batch) < 128:
                        try:
                            batch.append(self.queue.get_nowait())
                        except queue.Empty:
                            break
                    if batch:
                        retry_start = self.clock()
                        self._update(in_flight=len(batch))
                if batch:
                    if store is not None:
                        try:
                            expired = [] if self.snapshot()['clock_discontinuity'] else [r for r in batch if r.received_at_ms < wall * 1000 - (self.event_retention_ms if isinstance(r, EventRecord) else self.retention_ms)]
                            active = [r for r in batch if r not in expired]
                            with self._lock:
                                losses = dict(self._losses)
                            counts = store.insert_batch(list(losses.values()) + active)
                            with self._lock:
                                for key, value in losses.items():
                                    if self._losses.get(key) is value:
                                        del self._losses[key]
                            self._count(**counts, dropped_expired=len(expired))
                            if self.snapshot()['state'] == 'degraded':
                                with self._lock:
                                    self._pending_events['history_writer_recovered'] = EventRecord.create(None, 'history_writer_recovered', int(self.wall()*1000), 'degraded', 'healthy', 'commit_succeeded')
                            self._update(state='healthy', last_commit_ms=int(self.wall() * 1000), last_error=None)
                            batch.clear()
                        except Exception as exc:
                            self._error(exc)
                    if batch and (self.clock() - retry_start >= self.retry_seconds or self._stop.is_set()):
                        self._count(dropped_storage_error=len(batch))
                        with self._lock:
                            for record in batch:
                                self._remember_loss(record)
                        batch.clear()
                    self._update(in_flight=len(batch))
                if store is not None and not batch and now >= next_retention and not self.snapshot()['clock_discontinuity']:
                    try:
                        deleted = store.prune(int(wall * 1000 - self.retention_ms), event_cutoff_ms=int(wall * 1000 - self.event_retention_ms))
                        self._count(retention_deleted=deleted)
                        self._update(last_retention_ms=int(wall * 1000))
                        checkpoint_start = self.clock()
                        store.connection.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                        wal_path = Path(str(self.path) + '-wal')
                        self._update(last_checkpoint_ms=int(wall * 1000),
                                     checkpoint_duration_ms=(self.clock() - checkpoint_start) * 1000,
                                     wal_bytes=wal_path.stat().st_size if wal_path.exists() else 0)
                        next_retention = now + (0.1 if deleted >= 1000 else 3600)
                    except Exception as exc:
                        self._error(exc)
                        next_retention = now + 3600
                if self.raw_enabled and store is not None and now >= next_raw_retention:
                    try:
                        # Quota eviction does not depend on wall time, so it continues after a clock jump.
                        deleted = 0 if self.snapshot()['clock_discontinuity'] else store.prune_raw(int(wall*1000-self.raw_retention_ms))
                        evicted = store.evict_raw_over_quota(self.raw_quota_bytes)
                        with self._lock:
                            self._raw_stats['retention_deleted'] += deleted
                            self._raw_stats['quota_evicted'] += evicted
                        next_raw_retention = now + (0.1 if deleted >= 1000 or evicted >= 1000 else 60)
                    except Exception as exc:
                        with self._lock:
                            self._raw_stats['last_error'] = type(exc).__name__
                        next_raw_retention = now + 60
                # One bounded page turn after live writes; no polling worker touches SQLite.
                for _ in range(16):
                    if not self._raw_once(store):
                        break
                with self._lock:
                    events = dict(self._pending_events)
                if store is not None and events:
                    try:
                        self._count(**store.insert_batch(list(events.values())))
                        with self._lock:
                            for key, event in events.items():
                                if self._pending_events.get(key) is event:
                                    del self._pending_events[key]
                    except Exception as exc:
                        self._error(exc)
                if self._stop.is_set():
                    with self._lock:
                        self._stats['dropped_storage_error'] += len(self._pending_events)
                        self._pending_events.clear()
                if batch:
                    self._stop.wait(0.1)
        except Exception as exc:
            self._error(exc)
            self._count(dropped_storage_error=len(batch))
            self._update(in_flight=0)
        finally:
            # Stop admission before draining so no raw future is enqueued after the drain.
            with self._lock:
                self._accepting = False
            while not self._raw_queue.empty():
                self._raw_once(None)
            if store is not None:
                store.close()
            with self._lock:
                self._stats['state'] = 'stopped'

    def close(self, timeout=5.0):
        with self._lock:
            self._accepting = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, timeout))
        state = self.snapshot()
        remaining = state['queue_depth'] + state['in_flight'] + state['pending_events'] + state['raw']['queue_depth'] + state['raw']['in_flight']
        return {'flushed': remaining == 0 and not (self._thread and self._thread.is_alive()) and
                state['dropped_storage_error'] == 0 and state['raw']['failed_pages'] == 0, 'remaining': remaining}
