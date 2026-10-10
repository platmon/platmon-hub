"""Daily/monthly report integrity, retention and weighted aggregation."""
import csv
import io
import json
import unittest
import zipfile

import test_export as fixtures
from hub.archive import archive_once
from hub.export import DAY_MS, ExportError, export_history
from hub.report_cli import monthly_report
from hub.reports import read_daily


class ReportTests(unittest.TestCase):
    setUp = fixtures.ExportTests.setUp
    tearDown = fixtures.ExportTests.tearDown

    def extra(self):
        for seq, received, cpu, hottest in ((2, 2500, 20, 50), (3, DAY_MS + 1000, 100, 60)):
            self.store.connection.execute('INSERT INTO core_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (fixtures.NODE, 'first', seq, received, 0, 5000, 'ready', cpu, None, 30, 40, hottest))

    def rows(self, archive, name):
        return list(csv.DictReader(io.StringIO(archive.read(name).decode('utf-8-sig'))))

    def test_observed_aggregates_null_counts_peak_time_and_generations(self):
        self.extra()
        export_history(self.db, self.root / 'report.zip', 0, 2 * DAY_MS, [fixtures.NODE], summary_only=True)
        with zipfile.ZipFile(self.root / 'report.zip') as z:
            self.assertFalse(any(n.startswith('measurements/') for n in z.namelist()))
            rows = self.rows(z, 'summary-monthly.csv')
            first = next(row for row in rows if row['instance_id'] == 'first')
            self.assertEqual(first['cpu_percent_average'], '40.0')
            self.assertEqual(first['cpu_percent_count'], '3')
            self.assertEqual(first['gpu_percent_count'], '0')
            self.assertEqual(first['gpu_percent_average'], '')
            self.assertEqual(first['hottest_at_ms'], str(DAY_MS + 1000))
            self.assertEqual(len(rows), 2)
            events = self.rows(z, 'event-summary.csv')
            self.assertEqual(events[0]['current_state'], 'offline')
            self.assertEqual(events[0]['recorded_count'], '1')
            self.assertEqual(self.rows(z, 'gap-details.csv')[0]['clipped_overlap_ms'], str(2 * DAY_MS - 500))

    def test_retained_monthly_report_survives_raw_archive_expiry(self):
        self.extra()
        directory = self.root / 'archives'
        for _ in range(2): archive_once(self.db, directory, now_ms=2 * DAY_MS)
        # Prune raw archives after 90 days but preserve one-year daily summaries.
        archive_once(self.db, directory, now_ms=100 * DAY_MS)
        self.assertFalse((directory / '1970-01-01.zip').exists())
        self.assertTrue((directory / 'summaries' / '1970-01-01.json').exists())
        result = monthly_report(directory, self.root / 'month.zip', '1970-01')
        self.assertEqual(result['available_dates'], ['1970-01-01', '1970-01-02'])
        self.assertEqual(len(result['missing_dates']), 29)
        with zipfile.ZipFile(self.root / 'month.zip') as z:
            row = next(r for r in self.rows(z, 'summary-monthly.csv') if r['node_id'] == fixtures.NODE and r['instance_id'] == 'first')
            self.assertEqual(float(row['cpu_percent_average']), 40)
            self.assertEqual(row['gpu_percent_count'], '0')
            self.assertEqual(row['gpu_percent_average'], '')
            self.assertEqual(len(self.rows(z, 'gap-details.csv')), 2)

    def test_summary_retention_and_existing_output(self):
        directory = self.root / 'archives'
        archive_once(self.db, directory, now_ms=2 * DAY_MS)
        archive_once(self.db, directory, now_ms=400 * DAY_MS)
        self.assertFalse((directory / 'summaries' / '1970-01-01.json').exists())
        output = self.root / 'month.zip'
        output.write_bytes(b'keep')
        with self.assertRaises(FileExistsError): monthly_report(directory, output, '1970-01')
        self.assertEqual(output.read_bytes(), b'keep')

    def test_corrupt_summary_fails_without_partial_report(self):
        directory = self.root / 'archives'
        archive_once(self.db, directory, now_ms=2 * DAY_MS)
        path = directory / 'summaries' / '1970-01-01.json'
        document = json.loads(path.read_text(encoding='utf-8'))
        document['metrics'][0]['cpu_percent_count'] = -1
        path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(ExportError): read_daily(path)
        with self.assertRaises(ExportError): monthly_report(directory, self.root / 'bad.zip', '1970-01')
        self.assertFalse((self.root / 'bad.zip').exists())
        self.assertFalse(list(self.root.glob('.platmon-report-*')))
