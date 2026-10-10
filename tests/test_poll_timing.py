"""Hub poll duration instrumentation is additive and monotonic-time based."""

import unittest
import uuid

from hub.registry import Node
from hub.runtime import Fleet
from hub.state import NodeState


class StepClock:
    def __init__(self):
        self.value = 3.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Agent:
    def __init__(self, clock):
        self.clock = clock

    def get(self, endpoint, path, deadline):
        self.clock.advance(0.025)
        if path == "/api/status":
            return {"schema_version": 1, "instance_id": "generation-1",
                    "state": "ready", "ready": True, "sample": {"sequence": 9}}
        if path == "/api/stats":
            return {"schema_version": 1, "sample": {"instance_id": "generation-1",
                    "sequence": 9, "data_age_ms": 100, "stale_after_ms": 5000},
                    "collectors": {"core": {"state": "ok"}}}
        return {"schema_version": 2, "instance_id": "generation-1", "groups": {}}


class DurationTests(unittest.TestCase):
    def test_core_and_independent_observation_durations(self):
        clock = StepClock()
        node = Node(str(uuid.uuid4()), "fixture", "http://127.0.0.1:9797")
        fleet = Fleet([node], client=Agent(clock), clock=clock)
        try:
            self.assertIsNone(fleet.get_node(node.node_id)["last_core_duration_ms"])
            fleet.core_once(node.node_id)
            core = fleet.list_nodes()[0]
            self.assertAlmostEqual(core["last_core_duration_ms"], 50.0)
            self.assertIsNotNone(core["last_core_completed_utc"])
            self.assertIsNone(core["last_optional_duration_ms"])
            fleet.observations_once(node.node_id)
            optional = fleet.get_node(node.node_id)
            self.assertAlmostEqual(optional["last_optional_duration_ms"], 25.0)
            self.assertIsNotNone(optional["last_optional_completed_utc"])
        finally:
            fleet.close()

    def test_duration_metadata_validation(self):
        node = Node(str(uuid.uuid4()), "fixture", "http://127.0.0.1:9797")
        state = NodeState(node)
        for invalid in (float("nan"), -1, float("inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                state.record_poll_duration("core", invalid)
        with self.assertRaises(ValueError):
            state.record_poll_duration("unknown", 2)


if __name__ == "__main__":
    unittest.main()
