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
