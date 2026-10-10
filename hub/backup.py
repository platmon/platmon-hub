"""Explicit operator backup using SQLite's consistent online snapshot API."""
import argparse
import json
import sqlite3
import time
from pathlib import Path

from .history.store import read_connection


def backup_database(source, destination, timeout=30):
    destination = Path(destination).resolve()
    # Exclusive creation refuses accidental replacement of an existing backup.
    with destination.open('xb'):
        pass
    reader = writer = None
    try:
        reader = read_connection(source)
        writer = sqlite3.connect(str(destination))
        deadline = time.monotonic() + timeout
        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise TimeoutError('backup deadline exceeded')
        reader.backup(writer, pages=128, progress=progress, sleep=0.05)
        if writer.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('backup failed integrity check')
    except BaseException:
        if writer is not None:
            writer.close()
            writer = None
        destination.unlink()
        raise
    finally:
        if reader is not None: reader.close()
        if writer is not None: writer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='platmon-hub backup')
    parser.add_argument('--db', required=True)
    parser.add_argument('--output', required=True, help='new destination file; never overwritten')
    args = parser.parse_args(argv)
    try:
        backup_database(args.db, args.output)
    except (OSError, sqlite3.Error, ValueError, TimeoutError) as exc:
        parser.exit(2, 'Backup failed: ' + type(exc).__name__ + '\n')
    print(json.dumps({'backup': str(Path(args.output).resolve()), 'verified': True}))
