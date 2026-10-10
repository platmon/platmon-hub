"""Monthly reports from retained daily summaries, without raw SQLite history."""
import argparse
import json
import math
import os
import tempfile
import time
import zipfile
from pathlib import Path

from .archive import MARKER
from .export import CsvMember, DAY_MS, ExportError, METRICS, identity
from .filelock import FileLock
from .reports import EVENT_COLUMNS, GAP_COLUMNS, SUMMARY_COLUMNS, period_bounds, read_daily


def monthly_report(directory, output, month, timeout=30):
    start, end = period_bounds(month, 'month')
    if start < 0 or month != time.strftime('%Y-%m', time.gmtime(start / 1000)):
        raise ExportError('month must be YYYY-MM')
    root = Path(directory).resolve()
    output = Path(output).resolve()
    if output.exists(): raise FileExistsError('output already exists')
    deadline = time.monotonic() + timeout
    manifest = {'format_version': 1, 'report': 'retained daily summaries', 'month_utc': month,
                'from_ms': start, 'to_ms': end, 'available_dates': [], 'missing_dates': [],
                'completeness': 'Observed samples and recorded transitions only; missing dates are unknown, not healthy or offline.',
                'files': []}
    budget = dict(bytes=0, rows=0, max_bytes=32 * 1024 * 1024, max_rows=200000)
    metrics, events = {}, {}
    with FileLock(root / '.archive.lock'):
        if (root / MARKER).read_text(encoding='utf-8') != 'platmon-hub archive format 1\n':
            raise ExportError('invalid archive ownership marker')
        with tempfile.TemporaryDirectory(dir=str(output.parent), prefix='.platmon-report-') as scratch:
            temporary = Path(scratch) / 'report.zip'
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                gaps = CsvMember(archive, 'gap-details.csv', ('period_utc',) + GAP_COLUMNS, budget)
                try:
                    today = int(time.time() * 1000) // DAY_MS * DAY_MS
                    for day in range(start, min(end, today), DAY_MS):
                        if time.monotonic() >= deadline: raise ExportError('report deadline exceeded')
                        date = time.strftime('%Y-%m-%d', time.gmtime(day / 1000))
                        path = root / 'summaries' / (date + '.json')
                        if not path.exists():
                            manifest['missing_dates'].append(date)
                            continue
                        document = read_daily(path)
                        manifest['available_dates'].append(date)
                        for row in document['metrics']:
                            identity(row['node_id'])
                            if row['period_utc'] != date or row['granularity'] != 'day': raise ExportError('invalid summary period')
                            key = (row['node_id'], row['instance_id'])
                            if len(metrics) >= 10000 and key not in metrics: raise ExportError('report group limit exceeded')
                            old = metrics.get(key)
                            if old is None:
                                old = dict(row, period_utc=month, granularity='month', from_ms=start, to_ms=end)
                                metrics[key] = old
                                continue
                            old['sample_count'] += row['sample_count']
                            old['first_received_at_ms'] = min(old['first_received_at_ms'], row['first_received_at_ms'])
                            old['last_received_at_ms'] = max(old['last_received_at_ms'], row['last_received_at_ms'])
                            peak = row['hottest_c_maximum']
                            previous = old['hottest_c_maximum']
                            if peak is not None and (previous is None or peak > previous or (peak == previous and row['hottest_at_ms'] < old['hottest_at_ms'])):
                                old['hottest_at_ms'] = row['hottest_at_ms']
                            for metric in METRICS:
                                count = row[metric + '_count']
                                previous_count = old[metric + '_count']
                                if count:
                                    old[metric + '_average'] = (row[metric + '_average'] * count + (old[metric + '_average'] or 0) * previous_count) / (count + previous_count)
                                    for stat, fn in (('minimum', min), ('maximum', max)):
                                        value = row[metric + '_' + stat]
                                        old[metric + '_' + stat] = value if old[metric + '_' + stat] is None else fn(old[metric + '_' + stat], value)
                                old[metric + '_count'] += count
                        for row in document['events']:
                            key = tuple(row[column] for column in EVENT_COLUMNS[1:-1])
                            if len(events) >= 10000 and key not in events: raise ExportError('event group limit exceeded')
                            events[key] = events.get(key, 0) + row['recorded_count']
                        for row in document['gaps']:
                            gaps.row((date,) + tuple(row[column] for column in GAP_COLUMNS))
                finally: manifest['files'].append(gaps.close())
                for name, columns, rows in [('summary-monthly.csv', SUMMARY_COLUMNS, (tuple(row[column] for column in SUMMARY_COLUMNS) for row in metrics.values())),
                                             ('event-summary.csv', EVENT_COLUMNS, ((month,) + key + (count,) for key, count in events.items()))]:
                    member = CsvMember(archive, name, columns, budget)
                    try:
                        for row in rows:
                            if any(isinstance(value, float) and not math.isfinite(value) for value in row): raise ExportError('invalid summary numbers')
                            member.row(row)
                    finally: manifest['files'].append(member.close())
                archive.writestr('manifest.json', json.dumps(manifest, allow_nan=False, indent=2))
            if time.monotonic() >= deadline: raise ExportError('report deadline exceeded')
            os.link(str(temporary), str(output))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(prog='platmon-hub report')
    parser.add_argument('--archive-dir', required=True)
    parser.add_argument('--month', required=True, help='UTC YYYY-MM')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try: result = monthly_report(args.archive_dir, args.output, args.month)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        parser.exit(2, 'Report failed: ' + type(exc).__name__ + '\n')
    print(json.dumps({'output': str(Path(args.output).resolve()), 'available_dates': len(result['available_dates']), 'missing_dates': len(result['missing_dates'])}))
