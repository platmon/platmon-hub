"""Keep portable replay smoke checks separate from controlled-host timing gates."""
import importlib.util
import unittest
from pathlib import Path


path = Path(__file__).resolve().parents[1]/'scripts'/'benchmark-replay.py'
spec = importlib.util.spec_from_file_location('benchmark_replay_test', path)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class ReplayBenchmarkTests(unittest.TestCase):
    def result(self, **values):
        result = dict(nodes=10, captured_nodes=10, live_nodes=10, integrity='ok',
                      peak_encoded_queue_bytes=1024, peak_rss_bytes=None,
                      dropped_queue_full=0, commit_p95_ms=735.7, cleanup_ms=42,
                      raw=dict(rejected=0, failed_pages=0, cursor_read_failures=0))
        result.update(values)
        return result

    def test_smoke_keeps_correctness_checks_and_reports_slow_host_separately(self):
        result = self.result()
        self.assertTrue(all(benchmark.acceptance_checks(result, smoke=True).values()))
        self.assertFalse(benchmark.acceptance_checks(result)['commit_latency'])
        self.assertTrue(all(benchmark.acceptance_checks(
            self.result(commit_p95_ms=249, cleanup_ms=249)).values()))
        self.assertFalse(benchmark.acceptance_checks(
            self.result(cleanup_ms=250))['cleanup_latency'])

    def test_smoke_rejects_capture_storage_and_resource_failures(self):
        for values in (dict(captured_nodes=9), dict(live_nodes=9), dict(integrity='broken'),
                       dict(peak_encoded_queue_bytes=8*1024**2+1),
                       dict(peak_rss_bytes=256*1024**2+1), dict(dropped_queue_full=1)):
            with self.subTest(values=values):
                self.assertFalse(all(benchmark.acceptance_checks(
                    self.result(**values), smoke=True).values()))
        for key in ('rejected', 'failed_pages', 'cursor_read_failures'):
            with self.subTest(counter=key):
                result = self.result()
                result['raw'][key] = 1
                self.assertFalse(all(benchmark.acceptance_checks(result, smoke=True).values()))
