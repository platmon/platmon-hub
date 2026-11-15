"""The long-soak smoke names the acceptance checks it failed (#84)."""
import importlib.util
import sys
import unittest
from pathlib import Path


def load():
    scripts=Path(__file__).resolve().parents[1]/'scripts'
    spec=importlib.util.spec_from_file_location('soak_fleet_test',scripts/'soak-fleet.py')
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    # soak-fleet.py imports benchmark-fleet.py from its own folder, as a script run there would.
    sys.path.insert(0,str(scripts))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))
    return module


class SoakChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.soak=load()

    def result(self,**changes):
        result=dict(integrity='ok',live_nodes=10,writer=dict(dropped_batches=0,queue=0))
        result.update(changes)
        return result

    def test_a_passing_run_fails_nothing(self):
        self.assertEqual(self.soak.failed_checks(self.result(),10,{},True),{})

    def test_each_failed_check_is_named_with_its_value(self):
        failed=self.soak.failed_checks(
            self.result(integrity='row 3 missing',live_nodes=9,writer=dict(dropped_batches=2,dropped_rows=0)),
            10,{'history_ms:503:history_unavailable':1},False)
        self.assertEqual(failed,{
            'integrity':'row 3 missing',
            'live_nodes':{'final':9,'expected':10},
            'request_failures':{'history_ms:503:history_unavailable':1},
            'registry_unchanged':False,
            'writer_dropped':{'dropped_batches':2},
        })


if __name__=='__main__':
    unittest.main()
