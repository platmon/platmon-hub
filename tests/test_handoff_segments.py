"""ADR-0004 stage 2: segment builder and offline handoff CLI."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from hub import handoff
from hub.__main__ import main
from hub.history.projector import EventRecord
from hub.history.store import HistoryStore
from test_backend import NODE_ID, INSTANCE
from test_replay import document, page

QUOTA = 64 * 1024**2


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db, self.spool = root / 'history.db', root / 'spool'
        self.spool.mkdir()
        HistoryStore(self.db).open().close()
        with self.store() as store:
            store.enable_handoff('now', 0)

    def store(self):
        class Opened:
            def __enter__(inner):
                inner.store = HistoryStore(self.db).open_for_maintenance()
                return inner.store
            def __exit__(inner, *args):
                inner.store.close()
        return Opened()

    def add(self, ids=(1, 2), after=0, received=1000):
        with self.store() as store:
            store.insert_raw_page(page(document(ids, after=after), after=after, received=received), QUOTA)
            store._insert_event(EventRecord.create(NODE_ID, 'core_state', received, None, 'live', 'core_current'))

    def build(self, **kwargs):
        return handoff.build_segment(self.db, self.spool, **kwargs)

    def members(self, result):
        path = next(self.spool.glob('*/' + result['segment']))
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            rows = {f['name']: [json.loads(line) for line in archive.read(f['name']).splitlines()]
                    for f in manifest['files']}
            for f in manifest['files']:
                data = archive.read(f['name'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), f['sha256'])
                self.assertEqual(data.count(b'\n'), f['rows'])
        return manifest, rows

    def test_segment_holds_decoded_rows_envelopes_and_chain_metadata(self):
        self.add()
        result = self.build(now_ms=5)
        manifest, rows = self.members(result)
        self.assertEqual((manifest['format'], manifest['format_version']), ('platmon-hub-handoff', 1))
        self.assertEqual((manifest['from_seq'], manifest['previous_sha256'], manifest['created_at_ms']), (0, None, 5))
        raw = rows['raw_publications.jsonl']
        self.assertEqual([r['publication_id'] for r in raw], [1, 2])
        self.assertEqual(raw[0]['data']['power'], {'rail': 0, 'missing': None})
        self.assertNotIn('payload_zlib', raw[0])
        self.assertEqual({r['digest'] for r in rows['raw_envelopes.jsonl']}, {r['envelope_digest'] for r in raw})
        self.assertEqual(len(rows['events.jsonl']), 1)
        self.assertEqual(rows['nodes.jsonl'], [])
        self.assertFalse(manifest['registry_snapshot'])
        with self.store() as store:
            state = store.handoff_state()
        self.assertEqual((state['delivered_seq'], state['last_segment']), (result['to_seq'], result['segment']))
        self.assertIsNone(self.build())
        # The next segment continues the chain.
        self.add((3,), after=2, received=2000)
        second = self.build()
        manifest, _ = self.members(second)
        self.assertEqual((manifest['from_seq'], manifest['previous_segment'], manifest['previous_sha256']),
                         (result['to_seq'], result['segment'], result['sha256']))

    def test_size_limit_splits_at_row_boundaries_into_contiguous_ranges(self):
        self.add(tuple(range(1, 11)))
        results = []
        while True:
            result = self.build(max_bytes=1)
            if result is None:
                break
            results.append(result)
        self.assertGreater(len(results), 5)
        for previous, current in zip(results, results[1:]):
            self.assertEqual(current['from_seq'], previous['to_seq'])

    def test_a_row_changed_before_delivery_is_delivered_once_at_its_latest_version(self):
        with self.store() as store:
            gap = store._gap(NODE_ID, 'core', INSTANCE, 100, None, 'unobserved')
            store.connection.execute('UPDATE gaps SET end_ms=200 WHERE id=?', (gap,))
        manifest, rows = self.members(self.build())
        self.assertEqual([g['end_ms'] for g in rows['gaps.jsonl']], [200])
        delivered = [r['handoff_seq'] for name, member in rows.items() if name not in ('raw_envelopes.jsonl', 'nodes.jsonl')
                     for r in member]
        # Superseded versions leave holes; the chain of ranges, not contiguity, proves completeness.
        self.assertLess(len(delivered), manifest['to_seq'] - manifest['from_seq'])

    def test_recovery_adopts_a_published_segment_whose_delivery_was_not_recorded(self):
        self.add()
        with patch('hub.history.store.HistoryStore.record_delivery', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): self.build()
        published = list(self.spool.glob('*/*.zip'))
        self.assertEqual(len(published), 1)
        adopted = self.build()
        self.assertTrue(adopted['adopted'])
        self.assertEqual(adopted['segment'], published[0].name)
        self.assertEqual(len(list(self.spool.glob('*/*.zip'))), 1)
        self.assertIsNone(self.build())

    def test_unexpected_or_foreign_spool_files_stop_handoff(self):
        self.add()
        first = self.build()
        directory = next(self.spool.glob('*/'))
        (directory / 'notes.txt').write_text('x')
        with self.assertRaisesRegex(handoff.HandoffError, 'unexpected file'): self.build()
        (directory / 'notes.txt').unlink()
        foreign = directory / handoff.segment_name(first['to_seq'] + 5, first['to_seq'] + 9)
        foreign.write_bytes(b'')
        with self.assertRaisesRegex(handoff.HandoffError, 'does not continue'): self.build()

    def test_requires_enabled_handoff_and_exclusive_database(self):
        with self.store() as store:
            store.disable_handoff()
        with self.assertRaisesRegex(handoff.HandoffError, 'not enabled'): self.build()
        with self.store() as store:
            store.enable_handoff('now', 1)
        running = HistoryStore(self.db).open()
        try:
            with self.assertRaisesRegex(Exception, 'history_database_in_use'): self.build()
        finally:
            running.close()

    def test_cli_status_enable_build_and_registry_snapshot(self):
        registry = Path(self.directory.name) / 'nodes.json'
        registry.write_text(json.dumps({'nodes': [dict(node_id=NODE_ID, name='n', endpoint='http://192.0.2.1:9797',
                                                        capture='sampled')]}), encoding='utf-8')
        self.add()
        output = StringIO()
        with redirect_stdout(output):
            main(['handoff', '--db', str(self.db), 'status'])
            main(['handoff', '--db', str(self.db), 'build', '--spool', str(self.spool), '--nodes', str(registry)])
        status, built = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(status['enabled'])
        self.assertGreater(status['undelivered_versions'], 0)
        _, rows = self.members(built)
        self.assertEqual(rows['nodes.jsonl'], [dict(node_id=NODE_ID, enabled=True, capture='sampled')])
        with patch('sys.stderr'), self.assertRaises(SystemExit) as caught:
            main(['handoff', '--db', str(self.db), 'build', '--spool', str(self.spool / 'missing')])
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
