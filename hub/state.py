"""Per-node projection: upstream state, transport, freshness and optional groups."""

import math
import time
from datetime import datetime, timezone
from threading import RLock

from . import protocol



def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _ratio_percent(group):
    if not isinstance(group, dict):
        return None
    used, total = group.get("used"), group.get("total")
    if not (_finite(used) and _finite(total) and total > 0 and 0 <= used <= total):
        return None
    return round(100 * used / total, 1)


def _overview(stats):
    """Small, validated projection for node cards; never infer absent values as zero."""
    result = {"cpu_percent": None, "gpu_percent": None, "memory_percent": None,
              "disk_percent": None, "hottest_c": None}
    if not isinstance(stats, dict):
        return result
    cpus = stats.get("cpu")
    if isinstance(cpus, list):
        readings = [cpu["usage"] for cpu in cpus if isinstance(cpu, dict)
                    and _finite(cpu.get("usage")) and 0 <= cpu["usage"] <= 100]
        if readings:
            result["cpu_percent"] = round(sum(readings) / len(readings), 1)
    gpu = stats.get("gpu")
    if isinstance(gpu, dict):
        usage = gpu.get("usage")
        if _finite(usage) and 0 <= usage <= 100:
            result["gpu_percent"] = usage
    result["memory_percent"] = _ratio_percent(stats.get("memory"))
    result["disk_percent"] = _ratio_percent(stats.get("disk"))
    temps = stats.get("temperature")
    if isinstance(temps, dict):
        valid = [t for t in temps.values() if _finite(t)]
        if valid:
            result["hottest_c"] = round(max(valid), 1)
    return result


class NodeState:
    def __init__(self, node, *, clock=time.monotonic, core_interval=5.0, status_deadline=2.0, stats_deadline=2.0):
        self.node = node
        self.clock = clock
        self.core_interval = core_interval
        self.grace_ms = 1000 * (core_interval + status_deadline + stats_deadline)
        self._lock = RLock()
        self._headline = "unknown"
        self._agent_state = None
        self._ready = False
        self._last_status = None
        self._last_stats = None
        self._last_stats_tick = None
        self._last_observations = None
        self._last_obs_tick = None
        self._instance_id = None
        self._sample_key = None
        self._failures = 0
        self._core_error = None
        self._optional_error = None
        self._last_success_utc = None
        self._last_core_poll_utc = None
        self._last_obs_poll_utc = None

    def _today(self):
        return datetime.now(timezone.utc).isoformat()

    def note_valid_status(self, document):
        """Record reachable agent status without claiming usable stats."""
        st = protocol.status(document)
        with self._lock:
            self._failures = 0
            self._core_error = None
            self._agent_state = st["state"]
            self._ready = st["ready"]
            self._last_status = st
            self._last_core_poll_utc = self._today()
            self._last_success_utc = self._last_core_poll_utc
            if self._instance_id != st["instance_id"]:
                self._sample_key = None
                self._last_stats = None
                self._last_stats_tick = None
                self._last_observations = None
                self._last_obs_tick = None
            self._instance_id = st["instance_id"]
            if not st["ready"]:
                self._headline = st["state"]

    def accept_core(self, document, metrics=None):
        """Commit validated core response; status and stats belong to one instance."""
        st = protocol.status(document)
        if st["ready"]:
            if metrics is None:
                raise protocol.ProtocolFailure("ready requires stats")
            protocol.stats(metrics, st["instance_id"])
        self.note_valid_status(st)
        if not st["ready"]:
            return
        with self._lock:
            new_key = (st["instance_id"], metrics["sample"]["sequence"])
            if self._sample_key is not None and new_key[1] < self._sample_key[1]:
                self._core_error = "out_of_order_sample"
                self._headline = "error"
                return
            if metrics["sample"]["data_age_ms"] > metrics["sample"]["stale_after_ms"]:
                self._core_error = "expired_sample"
                self._headline = "stale"
                return
            self._sample_key = new_key
            self._last_stats = metrics
            self._last_stats_tick = self.clock()
            self._headline = "partial" if st["state"] == "degraded" else "live"

    def fail_core(self, failure):
        if not isinstance(failure, protocol.AgentFailure):
            raise TypeError("expected classified agent failure")
        with self._lock:
            self._core_error = failure.category
            self._last_core_poll_utc = self._today()
            if isinstance(failure, protocol.TransportFailure):
                self._failures += 1
                if self._failures >= 2:
                    self._headline = "offline"
                else:
                    self._headline = "stale" if (self._last_stats or self._agent_state in ("ready", "degraded", "stale")) else "unknown"
            elif isinstance(failure, protocol.NoCurrentData):
                self._headline = "stale"
            else:
                self._headline = failure.category

    def accept_observations(self, document):
        with self._lock:
            if self._instance_id is None:
                self._optional_error = "no_agent_instance"
                return
            protocol.observations(document, self._instance_id)
            if self._last_observations is not None:
                old = self._last_observations["groups"]
                for name, group in document["groups"].items():
                    previous = old.get(name, {}).get("observation")
                    current = group.get("observation")
                    if (previous is not None and current is not None and
                            current["id"] < previous["id"]):
                        self._optional_error = "out_of_order_observation"
                        self._last_obs_poll_utc = self._today()
                        return
            self._last_observations = document
            self._last_obs_tick = self.clock()
            self._last_obs_poll_utc = self._today()
            self._optional_error = None

    def fail_observations(self, failure):
        with self._lock:
            self._optional_error = failure.category
            self._last_obs_poll_utc = self._today()

    def snapshot(self, *, include_details=True):
        """Return an isolated JSON-compatible view; no mutable internal references leak."""
        import copy
        with self._lock:
            now = self.clock()
            headline = self._headline
            data_age = None
            metrics = None
            if self._last_stats is not None:
                data_age = self._last_stats["sample"]["data_age_ms"] + max(0, now - self._last_stats_tick) * 1000
                current = headline in ("live", "partial") and data_age <= self._last_stats["sample"]["stale_after_ms"] + self.grace_ms
                if not current and headline in ("live", "partial"):
                    headline = "stale"
                # Historical payload explicitly noncurrent; do not label it live.
                if include_details:
                    metrics = copy.deepcopy(self._last_stats)
            groups = None
            if include_details and self._last_observations is not None:
                groups = copy.deepcopy(self._last_observations["groups"])
                delta = max(0, now - self._last_obs_tick) * 1000
                for group in groups.values():
                    obs = group.get("observation")
                    if obs is not None:
                        obs["data_age_ms"] += delta
                        if obs["data_age_ms"] > group["stale_after_ms"]:
                            group["state"] = "stale"
                            obs["stale"] = True
            return {
                "node_id": self.node.node_id,
                "name": self.node.name,
                "enabled": self.node.enabled,
                "headline": headline if self.node.enabled else "disabled",
                "agent_state": self._agent_state,
                "ready": self._ready if headline in ("live", "partial") else False,
                "instance_id": self._instance_id,
                "sample_sequence": self._sample_key[1] if self._sample_key else None,
                "data_age_ms": round(data_age, 2) if data_age is not None else None,
                "data_current": headline in ("live", "partial") and self.node.enabled,
                "consecutive_transport_failures": self._failures,
                "core_error": self._core_error,
                "optional_error": self._optional_error,
                "last_core_poll_utc": self._last_core_poll_utc,
                "last_optional_poll_utc": self._last_obs_poll_utc,
                "last_success_utc": self._last_success_utc,
                "overview": _overview(self._last_stats),
                "stats": metrics,
                "observations": groups,
            }
