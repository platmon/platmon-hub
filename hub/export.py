"""Bounded, read-only history exports with a versioned CSV/ZIP contract."""
import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .history.schema import VERSION
from .history.store import read_connection

FORMAT_VERSION = 1
DAY_MS = 86400000
METRICS = ('cpu_percent', 'gpu_percent', 'memory_percent', 'disk_percent', 'hottest_c')
CORE = ('node_id', 'instance_id', 'sequence', 'received_at_ms', 'data_age_ms',
        'stale_after_ms', 'core_state') + METRICS
EVENTS = ('node_id', 'event_type', 'received_at_ms', 'previous_state',
          'current_state', 'reason_code', 'event_id')
GAPS = ('id', 'node_id', 'scope', 'instance_id', 'start_ms', 'end_ms', 'reason', 'approximate')
OBSERVATIONS = ('node_id', 'instance_id', 'group_name', 'observation_id',
                'received_at_ms', 'data_age_ms', 'stale_after_ms', 'state', 'data_json')


class ExportError(ValueError):
    """Invalid request or bounded export failure; no published output."""


def identity(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError) as exc:
        raise ExportError('node_id must be a canonical UUID') from exc
    return value


def utc(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def validate_range(start, end):
    if (type(start) is not int or type(end) is not int or start < 0 or
            end <= start or end - start > 31 * DAY_MS or end > 253402300799000):
        raise ExportError('range must be nonnegative, half-open and at most 31 days')


def spreadsheet_cell(value):
    # CSV quoting alone does not prevent spreadsheet formula execution. Prefix
    # risky text, including leading whitespace/control characters, with apostrophe.
    if isinstance(value, str) and value and (value[0] in "'=+-@" or
            value[0].isspace() or ord(value[0]) < 32):
        return "'" + value
    return value


class CsvMember:
    def __init__(self, archive, name, columns, budget):
        self.raw = archive.open(name, 'w', force_zip64=True)
        self.hash = hashlib.sha256()
        self.budget = budget
        self.count = 0
        self.size = 0
        self.name = name
        self.buffer = io.StringIO(newline='')
        self.writer = csv.writer(self.buffer, lineterminator='\r\n')
        try:
            self.write_bytes(b'\xef\xbb\xbf')
            self.line(columns)
        except BaseException:
            self.raw.close()
            raise

    def write_bytes(self, data):
        self.budget['bytes'] += len(data)
        if self.budget['bytes'] > self.budget['max_bytes']:
            raise ExportError('uncompressed output limit exceeded')
        self.raw.write(data)
        self.hash.update(data)
        self.size += len(data)

    def line(self, values):
        self.buffer.seek(0)
        self.buffer.truncate(0)
        self.writer.writerow([spreadsheet_cell(v) for v in values])
        self.write_bytes(self.buffer.getvalue().encode('utf-8'))

    def row(self, values):
        self.budget['rows'] += 1
        if self.budget['rows'] > self.budget['max_rows']:
            raise ExportError('row limit exceeded')
        self.count += 1
        self.line(values)

    def close(self):
        self.raw.close()
        return {'name': self.name, 'rows': self.count, 'bytes': self.size,
                'sha256': self.hash.hexdigest()}


def export_history(database, output, start, end, node_ids=(), observations=False,
                   timeout=30, max_rows=5000000, max_bytes=512 * 1024 * 1024, reports=False, summary_only=False):
    """Stream one consistent SQLite read transaction into an exclusive ZIP file."""
    validate_range(start, end)
    nodes = tuple(dict.fromkeys(identity(n) for n in node_ids))
    if len(nodes) > 256 or not 0 < timeout <= 120 or not 0 < max_rows <= 5000000 or not 0 < max_bytes <= 512 * 1024 * 1024:
        raise ExportError('invalid export budget')
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('output already exists')
    deadline = time.monotonic() + timeout
    budget = dict(rows=0, bytes=0, max_rows=max_rows, max_bytes=max_bytes)
    connection = None
    temp_path = None
    def check():
        if time.monotonic() >= deadline:
            raise ExportError('export deadline exceeded')
        return 0
    manifest = {'format_version': FORMAT_VERSION, 'database_schema_version': VERSION,
                'generated_at_utc': utc(int(time.time() * 1000)),
                'from_ms': start, 'to_ms': end, 'range': '[from_ms,to_ms)',
                'timestamp': 'Hub receipt UTC; not Agent collection time',
                'null': 'empty CSV cell; numeric zero remains 0',
                'text_escape': 'apostrophe prefixed to text starting with formula markers, whitespace/control characters or an apostrophe',
                'units': {m: ('degrees Celsius' if m == 'hottest_c' else 'percent') for m in METRICS},
                'observations_included': bool(observations and not summary_only), 'node_filter': list(nodes),
                'reports_included': bool(reports or summary_only), 'summary_only': bool(summary_only),
                'completeness': 'Persisted observations only; missing or expired data is not reconstructed. Open gaps may extend beyond the range.',
                'files': []}
    try:
        connection = read_connection(database)
        connection.set_progress_handler(check, 1000)
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            raise ExportError('unsupported history schema; export does not migrate databases')
        with tempfile.NamedTemporaryFile(dir=str(output.parent), prefix='.platmon-export-', suffix='.tmp', delete=False) as handle:
            temp_path = Path(handle.name)
        with zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def write_query(name, columns, sql, args):
                check()
                member = CsvMember(archive, name, columns, budget)
                try:
                    for row in connection.execute(sql, args):
                        check()
                        member.row(row)
                finally:
                    manifest['files'].append(member.close())
            # Group by canonical identity, UTC day and bounded parts. The cursor
            # streams; neither all rows nor all devices' members stay in memory.
            condition = 'received_at_ms >= ? AND received_at_ms < ?'
            params = (start, end)
            if nodes:
                condition += ' AND node_id IN (' + ','.join('?' for _ in nodes) + ')'
                params += nodes
            selected = () if summary_only else connection.execute('SELECT DISTINCT node_id FROM core_samples WHERE ' + condition, params)
            for (node,) in selected:
                identity(node)
                member = None
                previous_day = None
                part = 0
                try:
                    for row in connection.execute('SELECT ' + ','.join(CORE) + ' FROM core_samples WHERE node_id=? AND received_at_ms>=? AND received_at_ms<? ORDER BY received_at_ms,instance_id,sequence', (node, start, end)):
                        check()
                        day = utc(row[3])[:10]
                        if member is None or day != previous_day or member.count >= 100000:
                            if member is not None:
                                manifest['files'].append(member.close())
                                member = None
                            part = part + 1 if day == previous_day else 1
                            previous_day = day
                            if len(manifest['files']) >= 10000:
                                raise ExportError('file count limit exceeded')
                            member = CsvMember(archive, 'measurements/' + node + '/' + day + '-' + str(part) + '.csv', CORE, budget)
                        member.row(row)
                finally:
                    if member is not None:
                        manifest['files'].append(member.close())
            write_query('events.csv', EVENTS, 'SELECT ' + ','.join(EVENTS) + ' FROM events WHERE ' + condition + ' ORDER BY received_at_ms,event_id', params)
            gap_condition = 'start_ms < ? AND (end_ms IS NULL OR end_ms > ?)'
            gap_params = (end, start)
            if nodes:
                gap_condition += ' AND node_id IN (' + ','.join('?' for _ in nodes) + ')'
                gap_params += nodes
            write_query('gaps.csv', GAPS, 'SELECT ' + ','.join(GAPS) + ' FROM gaps WHERE ' + gap_condition + ' ORDER BY start_ms,id', gap_params)
            if observations and not summary_only:
                write_query('observations.csv', OBSERVATIONS, 'SELECT ' + ','.join(OBSERVATIONS) + ' FROM observations WHERE ' + condition + ' ORDER BY received_at_ms,node_id,group_name,observation_id', params)
            if reports or summary_only:
                from .reports import write_reports
                write_reports(connection, archive, manifest, budget, start, end, nodes, check, int(time.time() * 1000))
            check()
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=True, allow_nan=False, indent=2).encode('utf-8'))
        connection.rollback()
        connection.close()
        connection = None
        check()
        # Same-directory hard link publishes a complete file atomically and
        # refuses replacement. Filesystems without hard links fail closed.
        os.link(str(temp_path), str(output))
        temp_path.unlink()
        temp_path = None
        return manifest
    except sqlite3.OperationalError as exc:
        if time.monotonic() >= deadline:
            raise ExportError('export deadline exceeded') from exc
        raise
    finally:
        if connection is not None:
            connection.close()
        if temp_path is not None:
            temp_path.unlink()


def main(argv=None, report=False):
    parser = argparse.ArgumentParser(prog='platmon-hub report' if report else 'platmon-hub export')
    parser.add_argument('--db', required=True)
    parser.add_argument('--output', required=True, help='new ZIP file; never overwritten')
    parser.add_argument('--from-ms', required=True, type=int)
    parser.add_argument('--to-ms', required=True, type=int)
    parser.add_argument('--node-id', action='append', default=[])
    parser.add_argument('--observations', action='store_true', help='include selected optional data, potentially containing private probe addresses')
    parser.add_argument('--reports', action='store_true', help='include daily/monthly observed-data summaries')
    parser.add_argument('--summary-only', action='store_true', help='omit raw Core rows')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--max-rows', type=int, default=5000000)
    parser.add_argument('--max-bytes', type=int, default=512 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        result = export_history(args.db, args.output, args.from_ms, args.to_ms, args.node_id, args.observations, args.timeout, args.max_rows, args.max_bytes, args.reports or report, args.summary_only or report)
    except (OSError, sqlite3.Error, ExportError) as exc:
        parser.exit(2, 'Export failed: ' + type(exc).__name__ + '\n')
    print(json.dumps({'output': str(Path(args.output).resolve()), 'format_version': FORMAT_VERSION,
                      'rows': sum(f['rows'] for f in result['files'])}))
