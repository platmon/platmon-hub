"""Bounded, consistent committed raw JSONL export with explicit time semantics."""

import argparse
import json
import os
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from .export import ExportError, identity, validate_range
from .history.schema import VERSION
from .history.store import read_connection


def export_raw(database, output, start, end, node_ids=(), *, timeout=30,
               max_rows=100000, max_bytes=512*1024**2):
    validate_range(start, end)
    nodes = tuple(dict.fromkeys(identity(n) for n in node_ids))
    if (len(nodes) > 256 or not 0 < timeout <= 120 or type(max_rows) is not int or not 0 < max_rows <= 1000000 or
            type(max_bytes) is not int or not 0 < max_bytes <= 512*1024**2):
        raise ExportError('invalid raw export budget')
    target = Path(output).resolve()
    if target.exists():
        raise FileExistsError('output already exists')
    deadline = time.monotonic()+timeout
    connection, temp = None, None
    count, size = 0, 0
    manifest = dict(format_version=1, database_schema_version=VERSION, from_ms=start, to_ms=end,
                    range='[from_ms,to_ms), filtered by Hub receipt UTC milliseconds', node_filter=list(nodes),
                    timestamps={'received_at_ms': 'Hub receipt UTC milliseconds',
                                'reported_started_at': 'Agent reported Unix seconds; clock may jump',
                                'reported_completed_at': 'Agent reported Unix seconds; clock may jump'},
                    payload_schemas={'core': 1, 'storage': 2, 'wifi': 2, 'battery': 2, 'probe': 2},
                    agent_contract='47e384970324d71d9590c231504350ec2d40c863 replay envelope schema 1',
                    units={'elapsed_ages_and_durations': 'milliseconds',
                           'cpu_gpu_npu_freq_and_max_freq': 'hertz',
                           'cpu_gpu_npu_usage': 'percent', 'temperature_values': 'degrees Celsius',
                           'power_rails': 'watts', 'fans_rpm': 'revolutions per minute',
                           'battery_fields': 'preserve explicit _ua/_uv/_uw/_uah/_uwh units (micro units)',
                           'sensor_meta': 'per-value unit and source metadata retained verbatim',
                           'other_fields': 'Agent-defined; no unit inference or conversion'},
                    completeness='Committed raw publications only. Missing ranges denote unavailable publications; gap_before denotes failed/unpublished core attempts. No interpolation. Restart loss ends may be unknown. Retention/quota may limit capture.',
                    cursors='Committed cursor snapshot for selected nodes, independent of receipt-time filter and raw pruning.',
                    files=[])
    def check():
        if time.monotonic() >= deadline:
            raise ExportError('raw export deadline exceeded')
        return 0
    def encode(value):
        return (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))+'\n').encode()
    try:
        connection = read_connection(database)
        connection.set_progress_handler(check, 1000)
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            raise ExportError('unsupported raw storage schema')
        with tempfile.NamedTemporaryFile(dir=str(target.parent), prefix='.platmon-raw-', delete=False) as handle:
            temp = Path(handle.name)
        condition = 'received_at_ms >= ? AND received_at_ms < ?'
        params = (start, end)
        node_condition = 'node_id IN (' + ','.join('?' for _ in nodes) + ')' if nodes else '1=1'
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for table, where, args in (
                    ('raw_publications', condition+' AND '+node_condition, params+nodes),
                    ('replay_losses', condition+' AND '+node_condition, params+nodes),
                    ('replay_cursors', node_condition, nodes)):
                check()
                query = connection.execute('SELECT * FROM '+table+' WHERE '+where+' ORDER BY received_at_ms,node_id,instance_id,stream', args)
                names = [col[0] for col in query.description]
                rows = 0
                with archive.open(table+'.jsonl', 'w', force_zip64=True) as member:
                    for values in query:
                        check()
                        row = dict(zip(names, values))
                        if table == 'raw_publications':
                            row['data'] = json.loads(row.pop('data_json'))
                            row['envelope'] = json.loads(row.pop('envelope_json'))
                        data = encode(row)
                        count += 1
                        size += len(data)
                        if count > max_rows or size > max_bytes:
                            raise ExportError('raw export row/byte budget exceeded')
                        member.write(data)
                        rows += 1
                manifest['files'].append(dict(name=table+'.jsonl', rows=rows))
            data = encode(manifest)
            if size+len(data) > max_bytes:
                raise ExportError('raw export byte budget exceeded')
            archive.writestr('manifest.json', data)
        check()
        connection.rollback()
        connection.close()
        connection = None
        os.link(str(temp), str(target))
        return manifest
    finally:
        if connection is not None:
            connection.close()
        if temp is not None:
            temp.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='platmon-hub raw-export')
    parser.add_argument('--db', required=True)
    parser.add_argument('--output', required=True, help='new ZIP containing complete JSONL publications, loss evidence and cursors')
    parser.add_argument('--from-ms', required=True, type=int)
    parser.add_argument('--to-ms', required=True, type=int)
    parser.add_argument('--node-id', action='append', default=[])
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--max-rows', type=int, default=100000)
    parser.add_argument('--max-bytes', type=int, default=512*1024**2)
    args = parser.parse_args(argv)
    try:
        result = export_raw(args.db, args.output, args.from_ms, args.to_ms, args.node_id,
                            timeout=args.timeout, max_rows=args.max_rows, max_bytes=args.max_bytes)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, 'Raw export failed: '+type(exc).__name__+'\n')
    print(json.dumps(dict(output=str(Path(args.output).resolve()), rows=sum(f['rows'] for f in result['files']))))
