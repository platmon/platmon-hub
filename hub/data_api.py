"""Bounded read-only downloads; export work runs in a separate process."""
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from .export import DAY_MS, ExportError, identity, validate_range


def selection(query):
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    if set(values) - {'from_ms', 'to_ms', 'node_id'} or any(len(v) != 1 for v in values.values()):
        raise ExportError('invalid query')
    try:
        start, end = int(values['from_ms'][0]), int(values['to_ms'][0])
    except (ValueError, KeyError) as exc:
        raise ExportError('invalid range') from exc
    validate_range(start, end)
    if end - start > 7 * DAY_MS:
        raise ExportError('web range must be at most 7 days')
    node = identity(values['node_id'][0]) if 'node_id' in values else None
    return start, end, node


def generate(database, output, start, end, node=None):
    command = [sys.executable, '-m', 'hub', 'export', '--db', str(Path(database).resolve()),
               '--output', str(output), '--from-ms', str(start), '--to-ms', str(end),
               '--timeout', '10', '--max-rows', '200000', '--max-bytes', str(32 * 1024 * 1024)]
    if node:
        command.extend(('--node-id', node))
    # subprocess.run kills and waits for only this owned child on timeout.
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=12, check=False)
    if result.returncode:
        raise ExportError('export failed; reduce range or use the local CLI')


def download(handler, query):
    try:
        start, end, node = selection(query)
    except ValueError:
        handler._reply(400, {'error': 'invalid_export_selection'})
        return
    history = handler.server.fleet.history
    if history is None or history.snapshot()['schema_version'] is None:
        handler._reply(503, {'error': 'history_unavailable'})
        return
    if not handler.server.export_slot.acquire(blocking=False):
        handler._reply(429, {'error': 'export_busy'})
        return
    try:
        with tempfile.TemporaryDirectory(prefix='platmon-download-') as directory:
            output = Path(directory) / 'measurements.zip'
            try:
                generate(history.path, output, start, end, node)
            except (OSError, ExportError, subprocess.TimeoutExpired):
                handler._reply(422, {'error': 'export_failed', 'message': 'Reduce the range or use the local export CLI.'})
                return
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/zip')
            handler.send_header('Content-Disposition', 'attachment; filename="platmon-data.zip"')
            handler.send_header('Content-Length', str(output.stat().st_size))
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.end_headers()
            with output.open('rb') as stream:
                while True:
                    data = stream.read(65536)
                    if not data:
                        break
                    handler.wfile.write(data)
    finally:
        handler.server.export_slot.release()
