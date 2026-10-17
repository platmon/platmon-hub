"""Synthetic platmon v1.3.0 additions over the pinned 1.2.0 fixture, never real device traffic.

Fields follow docs/reference/api.md and replay.md at upstream tag v1.3.0
(e767176a8d5cbeb0c6bf4d04e71ceca9344b65b7).
"""
import copy
import json
import unittest
from pathlib import Path

from hub import protocol
from hub.registry import Node
from hub.state import NodeState
from test_backend import FakeClock, NODE_ID


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures' / 'agent-v1.2.0.json').read_text(encoding='utf-8'))


LEVELS = {'cpu': [80.0, 90.0], 'gpu': [80.0, 90.0], 'memory': [80.0, 90.0], 'filesystem': [80.0, 90.0],
          'temperature': [80.0, 90.0], 'battery': [20.0, 10.0], 'wifi': [-70.0, -80.0]}


class Agent130Tests(unittest.TestCase):
    def setUp(self):
        self.document = fixture()
        self.document['status']['levels'] = copy.deepcopy(LEVELS)
        self.state = NodeState(Node(NODE_ID, 'Synthetic 1.3.0 host', 'http://192.0.2.1:9797'), clock=FakeClock())

    def test_status_levels_with_low_bad_pairs_are_ignored_additive_keys(self):
        status = protocol.status(self.document['status'])
        self.assertEqual(status['levels']['battery'], [20.0, 10.0])
        accepted = self.state.accept_core(self.document['status'], self.document['stats'], received_at_ms=1000)
        self.assertTrue(accepted.record.valid())
        self.assertEqual(self.state.snapshot()['headline'], 'live')

    def test_live_schemas_are_unchanged(self):
        self.assertEqual(self.document['status']['schema_version'], 1)
        self.assertEqual(self.document['stats']['schema_version'], 1)
        self.assertEqual(self.document['observations']['schema_version'], 2)
        protocol.observations(self.document['observations'], self.document['status']['instance_id'])


if __name__ == '__main__':
    unittest.main()
