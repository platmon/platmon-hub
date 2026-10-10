"""Ensure Windows RSS instrumentation does not retain one ctypes type per sample."""
import ctypes
import importlib.util
import os
import sys
import unittest
from pathlib import Path


class BenchmarkMemoryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'native Windows process memory API')
    def test_repeated_rss_reads_keep_one_pointer_type(self):
        path=Path(__file__).resolve().parents[1]/'scripts'/'benchmark-fleet.py'
        spec=importlib.util.spec_from_file_location('benchmark_memory_test',path)
        module=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=module
        spec.loader.exec_module(module)
        first=module.rss_mib()
        cache_size=len(ctypes._pointer_type_cache)
        for _ in range(1000):
            self.assertGreater(module.rss_mib(),0)
        self.assertGreater(first,0)
        self.assertEqual(len(ctypes._pointer_type_cache),cache_size)
        self.assertEqual(module._windows_rss_reader.cache_info().misses,1)
