"""Opt-in daily archives, owned-directory retention and bounded worker process."""
import argparse
import json
import logging
import os
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .export import DAY_MS, ExportError, utc
from .filelock import FileLock
from .history.store import read_connection

MARKER = '.platmon-archives-v1'


def owned_archives(root):
    result = []
    for path in root.glob('????-??-??.zip'):
        if path.is_symlink():
            continue
        try:
            day = datetime.strptime(path.stem, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            start = int(day.timestamp() * 1000)
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo('manifest.json')
                if info.file_size > 4 * 1024 * 1024:
                    continue
                manifest = json.loads(archive.read(info))
            if manifest.get('format_version') == 1 and manifest.get('from_ms') == start and manifest.get('to_ms') == start + DAY_MS:
                result.append((start, path))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
    return sorted(result)


def run_worker(database, output, start, stop=None, observations=False):
    command = [sys.executable, '-m', 'hub', 'export', '--db', str(Path(database).resolve()),
               '--output', str(output), '--from-ms', str(start), '--to-ms', str(start + DAY_MS)]
    if observations: command.append('--observations')
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 35
        while child.poll() is None:
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                raise ExportError('archive worker cancelled or timed out')
            try: child.wait(timeout=0.25)
            except subprocess.TimeoutExpired: pass
        if child.returncode:
            raise ExportError('archive worker failed')
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)


def archive_once(database, directory, now_ms=None, retention_days=90,
                 quota_bytes=10 * 1024 ** 3, catchup_days=7, stop=None, observations=False):
    """Archive at most one complete UTC day, oldest available first."""
    if (type(retention_days) is not int or not 1 <= retention_days <= 3650 or
            type(catchup_days) is not int or not 1 <= catchup_days <= 31 or
            type(quota_bytes) is not int or not 1 <= quota_bytes <= 1024 ** 4):
        raise ExportError('invalid archive policy')
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    today = now_ms // DAY_MS * DAY_MS
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / '.archive.lock'):
        marker = root / MARKER
        if not marker.exists():
            if any(p.name != '.archive.lock' for p in root.iterdir()):
                raise ExportError('archive directory must be new or empty')
            with marker.open('x', encoding='utf-8') as stream: stream.write('platmon-hub archive format 1\n')
        elif marker.read_text(encoding='utf-8') != 'platmon-hub archive format 1\n':
            raise ExportError('invalid archive ownership marker')
        owned = owned_archives(root)
        for start, path in owned:
            if start + DAY_MS <= now_ms - retention_days * DAY_MS:
                path.unlink()
        connection = read_connection(database)
        try:
            connection.set_progress_handler(lambda: 1 if stop is not None and stop.is_set() else 0, 1000)
            earliest = connection.execute('SELECT min(t) FROM (SELECT min(received_at_ms) t FROM core_samples UNION ALL SELECT min(received_at_ms) FROM events)').fetchone()[0]
        finally: connection.close()
        if earliest is None:
            return {'state': 'idle', 'created': None, 'reason': 'no_persisted_data'}
        checkpoint = root / '.archive-state.json'
        last_day = None
        if checkpoint.exists():
            if checkpoint.stat().st_size > 1024:
                raise ExportError('invalid archive checkpoint')
            state = json.loads(checkpoint.read_text(encoding='utf-8'))
            last_day = state.get('last_day_ms')
            if type(last_day) is not int or last_day < 0 or last_day % DAY_MS:
                raise ExportError('invalid archive checkpoint')
        first = max(today - catchup_days * DAY_MS, earliest // DAY_MS * DAY_MS,
                    0 if last_day is None else last_day + DAY_MS)
        for start in range(first, today, DAY_MS):
            output = root / (utc(start)[:10] + '.zip')
            if output.exists():
                if output not in {p for _, p in owned_archives(root)}:
                    raise ExportError('existing daily archive is not recognized')
                continue
            # This owned scratch directory is cleaned even when its child is
            # killed during Hub shutdown. Publication is complete and exclusive.
            with tempfile.TemporaryDirectory(dir=str(root), prefix='.job-') as scratch:
                temporary = Path(scratch) / 'daily.zip'
                run_worker(database, temporary, start, stop, observations)
                size = temporary.stat().st_size
                if size > quota_bytes:
                    raise ExportError('daily archive exceeds disk quota')
                remaining = owned_archives(root)
                total = sum(p.stat().st_size for _, p in remaining)
                while total + size > quota_bytes and remaining:
                    _, oldest = remaining.pop(0)
                    total -= oldest.stat().st_size
                    oldest.unlink()
                os.link(str(temporary), str(output))
            state_temp = root / '.archive-state.tmp'
            with state_temp.open('w', encoding='utf-8') as stream:
                json.dump({'last_day_ms': start}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(state_temp), str(checkpoint))
            return {'state': 'ready', 'created': output.name, 'bytes': size}
        return {'state': 'idle', 'created': None, 'reason': 'caught_up'}


class ArchiveScheduler:
    def __init__(self, database, directory, retention_days=90, quota_bytes=10 * 1024 ** 3, catchup_days=7):
        self.database, self.directory = database, directory
        self.policy = dict(retention_days=retention_days, quota_bytes=quota_bytes, catchup_days=catchup_days)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.status = {'enabled': True, 'state': 'starting', 'last_success_ms': None, 'failures': 0}
        self.thread = threading.Thread(target=self._run, name='history-archive', daemon=True)

    def start(self): self.thread.start()

    def snapshot(self):
        with self.lock: return dict(self.status)

    def _run(self):
        while not self.stop.is_set():
            delay = 3600
            try:
                result = archive_once(self.database, self.directory, stop=self.stop, **self.policy)
                with self.lock:
                    self.status.pop("error", None)
                    self.status.update(result)
                    if result['created']:
                        self.status['last_success_ms'] = int(time.time() * 1000)
                        delay = 60
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
                if self.stop.is_set(): break
                with self.lock:
                    self.status.update(state='error', error=type(exc).__name__)
                    self.status['failures'] += 1
                logging.getLogger('hub.archive').warning('daily archive failed (%s)', type(exc).__name__)
                delay = 60
            if self.stop.wait(delay): break

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=4)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='platmon-hub archive')
    parser.add_argument('--db', required=True)
    parser.add_argument('--directory', required=True, help='dedicated new/empty archive directory')
    parser.add_argument('--retention-days', type=int, default=90)
    parser.add_argument('--quota-mib', type=int, default=10240)
    parser.add_argument('--catchup-days', type=int, default=7)
    parser.add_argument('--observations', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = archive_once(args.db, args.directory, retention_days=args.retention_days,
                              quota_bytes=args.quota_mib * 1024 ** 2, catchup_days=args.catchup_days,
                              observations=args.observations)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        parser.exit(2, 'Archive failed: ' + type(exc).__name__ + '\n')
    print(json.dumps(result))
