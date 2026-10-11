"""Reports of observed samples; never infer uptime from missing data."""
import calendar
import hashlib
import json
import math
from datetime import datetime, timezone

from .export import CsvMember, DAY_MS, ExportError, METRICS, identity

STAT_NAMES = ('count', 'average', 'minimum', 'maximum')
SUMMARY_COLUMNS = ('period_utc', 'granularity', 'node_id', 'instance_id', 'from_ms', 'to_ms',
                   'sample_count', 'first_received_at_ms', 'last_received_at_ms', 'hottest_at_ms') + tuple(m + '_' + s for m in METRICS for s in STAT_NAMES)
EVENT_COLUMNS = ('period_utc', 'node_id', 'event_type', 'previous_state', 'current_state', 'reason_code', 'recorded_count')
GAP_COLUMNS = ('id', 'node_id', 'scope', 'instance_id', 'start_ms', 'end_ms', 'reason', 'approximate', 'clipped_overlap_ms')


def where(start, end, nodes):
    condition = 'received_at_ms>=? AND received_at_ms<?'
    args = (start, end)
    if nodes:
        condition += ' AND node_id IN (' + ','.join('?' for _ in nodes) + ')'
        args += tuple(nodes)
    return condition, args


def period_bounds(period, grain):
    parsed = datetime.strptime(period, '%Y-%m-%d' if grain == 'day' else '%Y-%m').replace(tzinfo=timezone.utc)
    start = int(parsed.timestamp() * 1000)
    return start, start + (1 if grain == 'day' else calendar.monthrange(parsed.year, parsed.month)[1]) * DAY_MS


def metric_rows(connection, start, end, nodes, grain):
    fmt = '%Y-%m-%d' if grain == 'day' else '%Y-%m'
    period = "strftime('" + fmt + "',received_at_ms/1000,'unixepoch')"
    condition, args = where(start, end, nodes)
    aggregates = ','.join(fn + '(' + metric + ')' for metric in METRICS for fn in ('count', 'avg', 'min', 'max'))
    query = ('SELECT node_id,instance_id,' + period + ' period,count(*),min(received_at_ms),max(received_at_ms),' + aggregates +
             ' FROM core_samples WHERE ' + condition + ' GROUP BY node_id,instance_id,period ORDER BY node_id,instance_id,period')
    for row in connection.execute(query, args):
        node, instance, date, count, first, last = row[:6]
        low, high = period_bounds(date, grain)
        low, high = max(low, start), min(high, end)
        hottest = row[6 + METRICS.index('hottest_c') * len(STAT_NAMES) + 3]
        peak_time = None
        if hottest is not None:
            peak_time = connection.execute('SELECT min(received_at_ms) FROM core_samples WHERE node_id=? AND instance_id=? AND received_at_ms>=? AND received_at_ms<? AND hottest_c=?', (node, instance, low, high, hottest)).fetchone()[0]
        yield (date, grain, node, instance, low, high, count, first, last, peak_time) + row[6:]


def event_rows(connection, start, end, nodes):
    condition, args = where(start, end, nodes)
    query = ("SELECT strftime('%Y-%m-%d',received_at_ms/1000,'unixepoch') period,node_id,event_type,previous_state,current_state,reason_code,count(*) FROM events WHERE " +
             condition + ' GROUP BY period,node_id,event_type,previous_state,current_state,reason_code ORDER BY period,node_id,event_type')
    yield from connection.execute(query, args)


def gap_rows(connection, start, end, nodes, as_of):
    condition = 'start_ms<? AND (end_ms IS NULL OR end_ms>?)'
    args = (end, start)
    if nodes:
        condition += ' AND node_id IN (' + ','.join('?' for _ in nodes) + ')'
        args += tuple(nodes)
    for row in connection.execute('SELECT id,node_id,scope,instance_id,start_ms,end_ms,reason,approximate FROM gaps WHERE ' + condition + ' ORDER BY start_ms,id', args):
        overlap = max(0, min(end, as_of, row[5] if row[5] is not None else as_of) - max(start, row[4]))
        yield row + (overlap,)


def write_reports(connection, archive, manifest, budget, start, end, nodes, check, as_of):
    # Daily archives also contain a bounded machine-readable summary so monthly
    # material remains available after raw archives expire.
    daily = {'summary_format_version': 2, 'from_ms': start, 'to_ms': end,
             'as_of_ms': as_of, 'metrics': [], 'events': [], 'gaps': []}
    sources = [('summary-daily.csv', SUMMARY_COLUMNS, metric_rows(connection, start, end, nodes, 'day'), 'metrics'),
               ('summary-monthly.csv', SUMMARY_COLUMNS, metric_rows(connection, start, end, nodes, 'month'), None),
               ('event-summary.csv', EVENT_COLUMNS, event_rows(connection, start, end, nodes), 'events'),
               ('gap-details.csv', GAP_COLUMNS, gap_rows(connection, start, end, nodes, as_of), 'gaps')]
    for name, columns, rows, key in sources:
        member = CsvMember(archive, name, columns, budget)
        try:
            for row in rows:
                check()
                member.row(row)
                if key:
                    if sum(len(daily[k]) for k in ('metrics', 'events', 'gaps')) >= 10000:
                        raise ExportError('summary detail limit exceeded')
                    daily[key].append(dict(zip(columns, row)))
        finally:
            manifest['files'].append(member.close())
    output = json.dumps(daily, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(output) > 4 * 1024 * 1024:
        raise ExportError('summary document limit exceeded')
    budget['bytes'] += len(output)
    if budget['bytes'] > budget['max_bytes']:
        raise ExportError('uncompressed output limit exceeded')
    archive.writestr('summary.json', output)
    manifest['files'].append({'name': 'summary.json', 'rows': 0, 'bytes': len(output), 'sha256': hashlib.sha256(output).hexdigest()})


def read_daily(path):
    """Only recognized complete-day summaries are eligible for retention/merge."""
    from pathlib import Path
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise ExportError('invalid daily summary')
    document = json.loads(path.read_text(encoding='utf-8'))
    start, end = period_bounds(path.stem, 'day')
    if (not isinstance(document, dict) or type(document.get('summary_format_version')) is not int or
            document['summary_format_version'] not in (1, 2) or document.get('from_ms') != start or document.get('to_ms') != end or
            any(not isinstance(document.get(key), list) for key in ('metrics', 'events', 'gaps')) or
            sum(len(document[key]) for key in ('metrics', 'events', 'gaps')) > 10000):
        raise ExportError('invalid daily summary')
    for row in document['metrics']:
        if document['summary_format_version'] == 1:
            legacy = set(SUMMARY_COLUMNS) - {'npu_percent_' + stat for stat in STAT_NAMES}
            if not isinstance(row, dict) or set(row) != legacy:
                raise ExportError('invalid legacy metric summary')
            row.update({'npu_percent_' + stat: 0 if stat == 'count' else None for stat in STAT_NAMES})
        if not isinstance(row, dict) or set(row) != set(SUMMARY_COLUMNS):
            raise ExportError('invalid metric summary')
        identity(row['node_id'])
        if (row['period_utc'] != path.stem or row['granularity'] != 'day' or
                not isinstance(row['instance_id'], str) or not 1 <= len(row['instance_id'].encode('utf-8')) <= 128 or
                row['from_ms'] != start or row['to_ms'] != end or
                type(row['sample_count']) is not int or not 1 <= row['sample_count'] <= 5000000):
            raise ExportError('invalid summary identity or bounds')
        for metric in METRICS:
            count = row[metric + '_count']
            values = [row[metric + '_' + stat] for stat in ('average', 'minimum', 'maximum')]
            if (type(count) is not int or not 0 <= count <= row['sample_count'] or
                    (count == 0 and any(v is not None for v in values)) or
                    (count and any(type(v) not in (int, float) or not math.isfinite(v) for v in values))):
                raise ExportError('invalid summary statistics')
            if count and not values[1] - 1e-9 * max(1, abs(values[1])) <= values[0] <= values[2] + 1e-9 * max(1, abs(values[2])):
                raise ExportError('invalid summary extrema')
        if (row['hottest_c_count'] > 0) != (row['hottest_at_ms'] is not None):
            raise ExportError('invalid summary peak timestamp')
        if (type(row['first_received_at_ms']) is not int or type(row['last_received_at_ms']) is not int or
                not start <= row['first_received_at_ms'] <= row['last_received_at_ms'] < end or
                (row['hottest_at_ms'] is not None and (type(row['hottest_at_ms']) is not int or not start <= row['hottest_at_ms'] < end))):
            raise ExportError('invalid summary timestamps')
    for row in document['events']:
        if not isinstance(row, dict) or set(row) != set(EVENT_COLUMNS) or row['period_utc'] != path.stem or type(row['recorded_count']) is not int or row['recorded_count'] < 1:
            raise ExportError('invalid event summary')
        if row['node_id'] is not None: identity(row['node_id'])
    for row in document['gaps']:
        if not isinstance(row, dict) or set(row) != set(GAP_COLUMNS) or type(row['clipped_overlap_ms']) is not int or not 0 <= row['clipped_overlap_ms'] <= DAY_MS:
            raise ExportError('invalid gap summary')
        identity(row['node_id'])
    return document


def owned_summaries(root):
    result = []
    for path in (root / 'summaries').glob('????-??-??.json'):
        try:
            document = read_daily(path)
            result.append((document['from_ms'], path))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result)


def retain_summary(zip_path, root, quota_bytes, retention_days, now_ms):
    """Persist daily report independently of the raw archive retention window."""
    import os
    import zipfile
    from pathlib import Path
    root = Path(root)
    from .archive import owned_archives
    with zipfile.ZipFile(zip_path) as archive:
        info = archive.getinfo('summary.json')
        if info.file_size > 4 * 1024 * 1024:
            raise ExportError('daily summary limit exceeded')
        data = archive.read(info)
    directory = root / 'summaries'
    directory.mkdir(exist_ok=True)
    destination = directory / (zip_path.stem + '.json')
    temporary = directory / '.summary.tmp'
    temporary.write_bytes(data)
    try:
        # Check the document against the intended UTC day before publication.
        document = json.loads(data)
        low, high = period_bounds(zip_path.stem, 'day')
        if document.get('from_ms') != low or document.get('to_ms') != high:
            raise ExportError('invalid summary boundaries')
        os.replace(str(temporary), str(destination))
    finally:
        if temporary.exists(): temporary.unlink()
    summaries = owned_summaries(root)
    for start, path in summaries:
        if start + DAY_MS <= now_ms - retention_days * DAY_MS:
            path.unlink()
    # Quota covers recognized raw ZIPs and daily summaries together. Preserve
    # the new day's two files; discard older raw files first, then old summaries.
    raw = owned_archives(root)
    summaries = owned_summaries(root)
    total = sum(path.stat().st_size for _, path in raw + summaries)
    candidates = [(day, path) for day, path in raw if path != zip_path] + [(day, path) for day, path in summaries if path != destination]
    for _, path in candidates:
        if total <= quota_bytes: break
        total -= path.stat().st_size
        path.unlink()
    if total > quota_bytes:
        destination.unlink()
        raise ExportError('daily archive and summary exceed quota')
