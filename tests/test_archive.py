"""Automatic archive tests use isolated SQLite fixtures and owned workers."""
import json
import threading
import unittest
import zipfile
from unittest.mock import patch

import test_export as fixtures
from hub.archive import ArchiveScheduler, archive_once, owned_archives, run_worker
from hub.export import DAY_MS, ExportError


class ArchiveTests(unittest.TestCase):
    setUp = fixtures.ExportTests.setUp
    tearDown = fixtures.ExportTests.tearDown

    def archive(self, **options):
        return archive_once(self.db, self.root / 'archives', now_ms=2 * DAY_MS, **options)

    def test_daily_snapshot_catchup_and_idempotence(self):
        first = self.archive()
        self.assertEqual(first['created'], '1970-01-01.zip')
        self.assertEqual(self.archive()['created'], '1970-01-02.zip')
        self.assertEqual(self.archive()['state'], 'idle')
        with zipfile.ZipFile(self.root / 'archives' / first['created']) as z:
            manifest = json.loads(z.read('manifest.json'))
            self.assertEqual(manifest['from_ms'], 0)
            self.assertEqual(manifest['to_ms'], DAY_MS)
        self.assertEqual(len(owned_archives(self.root / 'archives')), 2)
        self.assertFalse(list((self.root / 'archives').glob('.job-*')))

    def test_refuses_unowned_directory_and_invalid_policy(self):
        root = self.root / 'archives'
        root.mkdir()
        (root / 'important.txt').write_text('keep', encoding='utf-8')
        with self.assertRaises(ExportError): self.archive()
        self.assertEqual((root / 'important.txt').read_text(encoding='utf-8'), 'keep')
        with self.assertRaises(ExportError): self.archive(retention_days=0)

    def test_quota_failure_does_not_publish_or_advance(self):
        with self.assertRaises(ExportError): self.archive(quota_bytes=1)
        root = self.root / 'archives'
        self.assertFalse(list(root.glob('*.zip')))
        self.assertFalse((root / '.archive-state.json').exists())
        self.assertFalse(list(root.glob('.job-*')))

    def test_quota_prunes_oldest_and_checkpoint_prevents_reexport_loop(self):
        first = self.archive()
        root = self.root / 'archives'
        size = (root / first['created']).stat().st_size
        second = self.archive(quota_bytes=size + 100)
        self.assertEqual(second['created'], '1970-01-02.zip')
        self.assertFalse((root / first['created']).exists())
        self.assertEqual(self.archive()['state'], 'idle')

    def test_age_retention_and_unrecognized_files(self):
        self.archive()
        root = self.root / 'archives'
        unknown = root / '1969-12-31.zip'
        unknown.write_bytes(b'not ours')
        archive_once(self.db, root, now_ms=100 * DAY_MS, retention_days=90)
        self.assertFalse((root / '1970-01-01.zip').exists())
        self.assertEqual(unknown.read_bytes(), b'not ours')

    def test_worker_cancel_is_owned_and_cleans_up(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(ExportError):
            run_worker(self.db, self.root / 'cancelled.zip', 0, stop=stop)
        self.assertFalse((self.root / 'cancelled.zip').exists())

    def test_scheduler_surfaces_failure_and_shutdown(self):
        scheduler = ArchiveScheduler(self.db, self.root / 'archives')
        seen = threading.Event()
        def fail(*args, **kwargs):
            seen.set()
            raise OSError('private path')
        with patch('hub.archive.archive_once', side_effect=fail):
            scheduler.start()
            self.assertTrue(seen.wait(2))
            scheduler.close()
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot['state'], 'error')
        self.assertEqual(snapshot['failures'], 1)
        self.assertNotIn('private path', str(snapshot))
        self.assertFalse(scheduler.thread.is_alive())
