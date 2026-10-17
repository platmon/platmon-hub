"""Bounded immutable records made from an accepted core sample."""

import math
import uuid
from dataclasses import dataclass, astuple

OBSERVATION_GROUPS = ('storage', 'wifi', 'probe', 'battery')
PRESSURE_COLUMNS = ('psi_cpu_some_avg10', 'psi_memory_some_avg10', 'psi_io_some_avg10')
RATE_COLUMNS = ('network_rx_busiest_bytes_per_s', 'network_tx_busiest_bytes_per_s',
                'disk_read_bytes_per_s', 'disk_write_bytes_per_s')
LOAD_COLUMNS = PRESSURE_COLUMNS + RATE_COLUMNS


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
    npu_percent: object = None
    psi_cpu_some_avg10: object = None
    psi_memory_some_avg10: object = None
    psi_io_some_avg10: object = None
    network_rx_busiest_bytes_per_s: object = None
    network_tx_busiest_bytes_per_s: object = None
    disk_read_bytes_per_s: object = None
    disk_write_bytes_per_s: object = None

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
                        for x in (self.cpu_percent, self.gpu_percent, self.memory_percent,
                                  self.disk_percent, self.npu_percent) +
                        tuple(getattr(self, name) for name in PRESSURE_COLUMNS)) and
                    all(x is None or (type(x) in (int, float) and math.isfinite(x) and x >= 0)
                        for x in (getattr(self, name) for name in RATE_COLUMNS)) and
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
        return (base.valid() and self.group_name in OBSERVATION_GROUPS and
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
                    self.scope in ('core',) + OBSERVATION_GROUPS and
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
    if name not in OBSERVATION_GROUPS or publication is None or group['state'] in ('starting', 'stale') or publication.get('stale') is True:
        return None
    data = group.get('data')
    if name == 'battery':
        payload = json.dumps(_battery_data(data), allow_nan=False, separators=(',', ':'))
        return ObservationRecord(node_id, instance_id, name, publication['id'], received_at_ms,
                                 publication['data_age_ms'], group['stale_after_ms'], group['state'], payload)
    rows_name, fields = spec[name]
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


def _battery_data(data):
    """Keep driver units, signed current/power and unknown booleans unchanged."""
    fields = ('name', 'present', 'percent', 'status', 'capacity_level', 'technology', 'health',
              'charge_now_uah', 'charge_full_uah', 'charge_full_design_uah',
              'energy_now_uwh', 'energy_full_uwh', 'energy_full_design_uwh',
              'current_ua', 'power_uw', 'voltage_uv', 'cycle_count')
    result = {}
    for key, selected in (('batteries', fields), ('external', ('name', 'type', 'online'))):
        rows = data.get(key) if isinstance(data, dict) else None
        result[key] = None
        if not isinstance(rows, list):
            continue
        result[key] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = {}
            for field in selected:
                value = row.get(field)
                if key == 'batteries' and row.get('present') is False and field not in ('name', 'present'):
                    value = None
                if field in ('present', 'online'):
                    value = value if type(value) is bool else None
                elif field in ('name', 'type', 'status', 'capacity_level', 'technology', 'health'):
                    value = value if isinstance(value, str) else None
                elif not (type(value) in (int, float) and math.isfinite(value)):
                    value = None
                elif field not in ('current_ua', 'power_uw') and value < 0:
                    value = None
                elif field == 'percent' and value > 100:
                    value = None
                item[field] = value
            result[key].append(item)
    return result


EVENT_TYPES = ('core_state', 'agent_restart', 'optional_error', 'optional_recovered',
               'history_writer_error', 'history_writer_recovered', 'history_queue_overflow', 'registry_changed', 'alert_transition')


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
