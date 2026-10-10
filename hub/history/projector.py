"""Bounded immutable records made from an accepted core sample."""

import math
import uuid
from dataclasses import dataclass, astuple


@dataclass(frozen=True)
class CoreRecord:
    node_id: str
    instance_id: str
    sequence: int
    received_at_ms: int
    data_age_ms: float
    stale_after_ms: float
    core_state: str
    cpu_percent: object
    gpu_percent: object
    memory_percent: object
    disk_percent: object
    hottest_c: object

    def values(self):
        return astuple(self)

    def valid(self):
        try:
            return (str(uuid.UUID(self.node_id)) == self.node_id and
                    1 <= len(self.instance_id.encode('utf-8')) <= 128 and
                    type(self.sequence) is int and 1 <= self.sequence <= 2**63 - 1 and
                    type(self.received_at_ms) is int and 0 <= self.received_at_ms <= 2**63 - 1 and
                    all(type(x) in (int, float) and math.isfinite(x)
                        for x in (self.data_age_ms, self.stale_after_ms)) and
                    0 <= self.data_age_ms <= self.stale_after_ms and self.stale_after_ms > 0 and
                    self.core_state in ('ready', 'degraded') and
                    all(x is None or (type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 100)
                        for x in self.values()[7:11]) and
                    (self.hottest_c is None or (type(self.hottest_c) in (int, float) and
                                               math.isfinite(self.hottest_c))))
        except (ValueError, TypeError, AttributeError, OverflowError):
            return False


@dataclass(frozen=True)
class Acceptance:
    disposition: str
    record: object = None


@dataclass(frozen=True)
class ObservationRecord:
    node_id: str
    instance_id: str
    group_name: str
    observation_id: int
    received_at_ms: int
    data_age_ms: float
    stale_after_ms: float
    state: str
    data_json: str

    def valid(self):
        base = CoreRecord(self.node_id, self.instance_id, self.observation_id,
                          self.received_at_ms, self.data_age_ms, self.stale_after_ms,
                          'ready', None, None, None, None, None)
        return (base.valid() and self.group_name in ('storage', 'wifi', 'probe') and
                self.state in ('ok', 'partial', 'error', 'unavailable') and
                len(self.data_json.encode('utf-8')) <= 16384)

    def values(self):
        return astuple(self)


@dataclass(frozen=True)
class ContinuityRecord:
    node_id: str
    scope: str
    instance_id: object
    received_at_ms: int
    reason: object
    silence_ms: float = 15000
    headline: object = None

    def valid(self):
        try:
            return (str(uuid.UUID(self.node_id)) == self.node_id and
                    self.scope in ('core', 'storage', 'wifi', 'probe') and
                    (self.instance_id is None or 1 <= len(self.instance_id.encode('utf-8')) <= 128) and
                    type(self.received_at_ms) is int and 0 <= self.received_at_ms < 2**63 and
                    self.reason in (None, 'transport', 'offline', 'stale', 'starting', 'error',
                                    'incompatible', 'absent', 'unavailable', 'partial', 'storage_loss',
                                    'unobserved', 'disabled') and math.isfinite(self.silence_ms) and self.silence_ms > 0)
        except (ValueError, TypeError, AttributeError):
            return False


def project_observation(node_id, instance_id, name, group, received_at_ms):
    """Persist selected, unit-preserving scalar fields; never copy an envelope."""
    import json
    spec = {
        'storage': ('filesystems', ('device', 'major', 'minor', 'total_bytes', 'used_bytes', 'available_bytes')),
        'wifi': ('interfaces', ('name', 'ifindex', 'connected', 'connection_state', 'signal_dbm', 'signal_raw', 'link_quality', 'noise_dbm')),
        'probe': ('targets', ('address', 'port', 'connect_ms', 'reason', 'attempts', 'failures', 'failure_ratio')),
    }
    publication = group.get('observation')
    if name not in spec or publication is None or group['state'] in ('starting', 'stale') or publication.get('stale') is True:
        return None
    rows_name, fields = spec[name]
    data = group.get('data')
    rows = data.get(rows_name) if isinstance(data, dict) else None
    projected = None
    if isinstance(rows, list):
        projected = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = {}
            for key in fields:
                value = row.get(key)
                if key == 'connected':
                    value = value if type(value) is bool else None
                elif key in ('device', 'name', 'address', 'connection_state', 'reason'):
                    value = value if isinstance(value, str) else None
                else:
                    value = value if type(value) in (int, float) and math.isfinite(value) else None
                item[key] = value
            projected.append(item)
    payload = json.dumps({rows_name: projected}, allow_nan=False, separators=(',', ':'))
    return ObservationRecord(node_id, instance_id, name, publication['id'], received_at_ms,
                             publication['data_age_ms'], group['stale_after_ms'], group['state'], payload)


EVENT_TYPES = ('core_state', 'agent_restart', 'optional_error', 'optional_recovered',
               'history_writer_error', 'history_writer_recovered', 'history_queue_overflow', 'registry_changed')


@dataclass(frozen=True)
class EventRecord:
    node_id: object
    event_type: str
    received_at_ms: int
    previous_state: object
    current_state: object
    reason_code: str
    event_id: str

    @classmethod
    def create(cls, node_id, event_type, received_at_ms, previous_state, current_state, reason_code):
        return cls(node_id, event_type, received_at_ms, previous_state, current_state, reason_code, str(uuid.uuid4()))

    def valid(self):
        try:
            return (self.event_type in EVENT_TYPES and str(uuid.UUID(self.event_id)) == self.event_id and
                    (self.node_id is None or str(uuid.UUID(self.node_id)) == self.node_id) and
                    type(self.received_at_ms) is int and 0 <= self.received_at_ms < 2**63 and
                    all(v is None or (isinstance(v, str) and len(v.encode('utf-8')) <= 128)
                        for v in (self.previous_state, self.current_state, self.reason_code)))
        except (ValueError, AttributeError, TypeError):
            return False

    def values(self):
        return astuple(self)
