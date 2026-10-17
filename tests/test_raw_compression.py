"""ADR-0006: zlib raw payloads with shared, content-addressed dictionaries."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from hub.history import codec
from hub.history.schema import VERSION
from hub.history.store import HistoryStore
from hub.history.writer import HistoryWriter
from hub.raw_export import export_raw
from test_backend import NODE_ID, INSTANCE
from test_replay import document, page


def rows(connection):
    return connection.execute(
        'SELECT publication_id,data_json,payload_zlib,dictionary_id,encoded_bytes,'
        '(SELECT content FROM raw_dictionaries d WHERE d.dictionary_id=p.dictionary_id) '
        'FROM raw_publications p ORDER BY stream,publication_id').fetchall()


class CodecTests(unittest.TestCase):
    def test_dictionary_per_stream_and_instance_with_drift_reselection(self):
        dictionaries = codec.Dictionaries()
        payload = json.dumps({'cpu': list(range(200)), 'name': 'x' * 400})
        blob, content, digest, failed = dictionaries.encode('n', 'i1', 'core', payload)
        self.assertFalse(failed)
        self.assertEqual(codec.decode('', blob, content), payload)
        self.assertEqual(dictionaries.encode('n', 'i1', 'core', payload)[2], digest)
        # A new instance takes its own first publication; identical content shares the dictionary.
        other = payload.replace('x', 'y')
        self.assertNotEqual(dictionaries.encode('n', 'i2', 'core', other)[2], digest)
        self.assertEqual(dictionaries.encode('n', 'i2', 'core', payload)[2],
                         codec.hashlib.sha256(other.encode()).hexdigest())
        dictionaries = codec.Dictionaries()
        first = dictionaries.encode('n', 'i', 'core', payload)[2]
        for _ in range(codec.BASELINE_ROWS):
            dictionaries.encode('n', 'i', 'core', payload)
        unrelated = json.dumps([str(n) * 7 for n in range(400)])
        for _ in range(codec.RECENT_ROWS):
            self.assertEqual(dictionaries.encode('n', 'i', 'core', unrelated)[2], first)
        self.assertNotEqual(dictionaries.encode('n', 'i', 'core', unrelated)[2], first)

    def test_incompressible_payload_is_stored_as_json_without_failure(self):
        self.assertEqual(codec.Dictionaries().encode('n', 'i', 'core', '1')[:2], (None, None))
        self.assertFalse(codec.Dictionaries().encode('n', 'i', 'core', '1')[3])

    def test_decode_rejects_missing_dictionary_and_truncation(self):
        blob = codec.compress(b'{"a":1}' * 50, b'{"a":1}')
        with self.assertRaises(ValueError): codec.decode('', blob, None)
        with self.assertRaises(ValueError): codec.decode('', blob[:-4], b'{"a":1}')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'raw.db'
        self.store = HistoryStore(self.path).open()
        self.addCleanup(self.store.close)
        self.c = self.store.connection

    def usage(self):
        return self.c.execute('SELECT encoded_bytes FROM raw_usage').fetchone()[0]

    def charged(self):
        return sum(self.c.execute('SELECT coalesce(sum(encoded_bytes),0) FROM ' + table).fetchone()[0]
                   for table in ('raw_publications', 'raw_envelopes', 'raw_dictionaries'))

    def test_byte_identical_round_trip_for_core_and_every_slow_group(self):
        pages = [page(document(tuple(range(1, 21))))]
        pages += [page(document(tuple(range(1, 21)), stream=name)) for name in ('storage', 'wifi', 'battery', 'probe')]
        expected = {}
        for value in pages:
            result = self.store.insert_raw_page(value, 64 * 1024**2)
            self.assertEqual(result['inserted'], 20)
            self.assertEqual(result['fallback'], 0)
            for pid, payload, *_ in value.records:
                expected[value.stream, pid] = payload
        stored = self.c.execute('SELECT stream,publication_id,data_json,payload_zlib,'
                                '(SELECT content FROM raw_dictionaries d WHERE d.dictionary_id=p.dictionary_id) '
                                'FROM raw_publications p').fetchall()
        self.assertEqual(len(stored), 100)
        for stream, pid, data_json, blob, content in stored:
            self.assertEqual(codec.decode(data_json, blob, content), expected[stream, pid])
            if blob is not None:
                self.assertEqual(data_json, '')
        self.assertGreater(sum(blob is not None for *_, blob, _ in stored), 0)
        self.assertEqual(self.usage(), self.charged())

    def test_verification_failure_falls_back_to_json(self):
        with patch('hub.history.codec.compress', return_value=None):
            result = self.store.insert_raw_page(page(document((1, 2))), 64 * 1024**2)
        self.assertEqual((result['compressed'], result['stored_json'], result['fallback']), (0, 2, 2))
        for _, data_json, blob, dictionary_id, size, _ in rows(self.c):
            self.assertIsNone(blob)
            self.assertIsNone(dictionary_id)
            self.assertEqual(size, len(data_json.encode()) + 256)
        self.assertEqual(self.c.execute('SELECT count(*) FROM raw_dictionaries').fetchone()[0], 0)
        self.assertEqual(self.usage(), self.charged())

    def test_dictionary_references_under_duplicates_retention_and_quota(self):
        self.store.insert_raw_page(page(document(tuple(range(1, 11))), received=1000), 64 * 1024**2)
        self.assertEqual(self.c.execute('SELECT count(*) FROM raw_dictionaries').fetchone()[0], 1)
        refs = self.c.execute('SELECT refs FROM raw_dictionaries').fetchone()[0]
        self.assertEqual(refs, sum(row[3] is not None for row in rows(self.c)))
        # A duplicate-only retry from a new process picks a new dictionary and leaves no orphan.
        self.store.dictionaries = codec.Dictionaries()
        retry = document(tuple(range(1, 11)))
        retry['records'][0]['data']['power']['rail'] = 7
        self.store.insert_raw_page(page(retry), 64 * 1024**2)
        self.assertEqual(self.c.execute('SELECT count(*), max(refs) FROM raw_dictionaries').fetchone(), (1, refs))
        self.assertEqual(self.usage(), self.charged())
        later = page(document((11, 12), after=10), after=10, received=5000)
        self.store.insert_raw_page(later, 64 * 1024**2)
        self.assertEqual(self.store.prune_raw(2000), 10)
        self.assertEqual(self.usage(), self.charged())
        self.assertEqual(self.c.execute('SELECT count(*) FROM raw_dictionaries').fetchone()[0], 1)
        while self.store.evict_raw_over_quota(0):
            pass
        self.assertEqual(self.c.execute('SELECT count(*) FROM raw_dictionaries').fetchone()[0], 0)
        self.assertEqual(self.usage(), 0)

    def test_quota_charges_stored_bytes(self):
        value = page(document(tuple(range(1, 31))))
        self.store.insert_raw_page(value, 64 * 1024**2)
        self.assertEqual(self.usage(), self.charged())
        self.assertLess(self.usage(), value.encoded_bytes)
        with self.assertRaisesRegex(ValueError, 'raw_quota'):
            self.store.insert_raw_page(page(document((31,), after=30), after=30), 1)
        self.assertEqual(self.usage(), self.charged())


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'raw.db'

    def test_v7_rows_migrate_and_export_with_compressed_rows(self):
        with patch('hub.history.store.MIGRATION_8', 'INVALID SQL;'):
            with self.assertRaises(sqlite3.Error): HistoryStore(self.path).open()
        legacy = page(document((1,)), received=1000)
        c = sqlite3.connect(self.path)
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 7)
        envelope = c.execute('INSERT INTO raw_envelopes (digest,envelope_json,encoded_bytes) VALUES (?,?,?)',
                             ('legacy', legacy.envelope_json, len(legacy.envelope_json) + 256)).lastrowid
        pid, payload, gap, start, end = legacy.records[0]
        c.execute('INSERT INTO raw_publications VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  (NODE_ID, INSTANCE, 'core', pid, 1000, start, end, int(gap), payload, envelope, len(payload) + 256))
        c.commit()
        c.close()
        store = HistoryStore(self.path).open()
        try:
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], VERSION)
            self.assertEqual(len(list(self.path.parent.glob('raw.db.v7-backup-*'))), 1)
            usage = store.connection.execute('SELECT encoded_bytes FROM raw_usage').fetchone()[0]
            self.assertEqual(usage, len(payload) + 256 + len(legacy.envelope_json) + 256)
            store.insert_raw_page(page(document((2, 3)), received=1500), 64 * 1024**2)
            compressed = store.connection.execute(
                'SELECT count(*) FROM raw_publications WHERE payload_zlib IS NOT NULL').fetchone()[0]
            self.assertEqual(compressed, 2)
        finally:
            store.close()
        output = self.path.with_suffix('.zip')
        export_raw(self.path, output, 0, 2000, [NODE_ID])
        with zipfile.ZipFile(output) as archive:
            exported = [json.loads(line) for line in archive.read('raw_publications.jsonl').splitlines()]
        self.assertEqual([row['publication_id'] for row in exported], [1, 2, 3])
        for row in exported:
            self.assertEqual(row['data']['sample']['sequence'], row['publication_id'])
            self.assertEqual(row['encoded_bytes'], len(json.dumps(row['data'], separators=(',', ':'))) + 256)
            self.assertNotIn('payload_zlib', row)
            self.assertNotIn('dictionary_id', row)

    def test_writer_reports_compression(self):
        writer = HistoryWriter(self.path, raw_retention_days=1, raw_quota_bytes=1024**2)
        writer.start()
        try:
            writer.submit_raw(page(document(tuple(range(1, 11))))).result(15)
            raw = writer.snapshot()['raw']
        finally:
            writer.close()
        self.assertEqual(raw['compressed_rows'] + raw['json_rows'], 10)
        self.assertEqual(raw['compression_fallback'], 0)
        self.assertEqual(raw['dictionaries'], 1)
        self.assertLess(raw['stored_logical_ratio'], 1)


if __name__ == '__main__':
    unittest.main()
