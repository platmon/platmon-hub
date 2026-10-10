"""Synchronous SQLite operations, owned exclusively by the history writer."""

import sqlite3
from pathlib import Path

from ..filelock import FileLock
from .schema import SQL, VERSION


class SchemaError(Exception):
    """Unsupported or unrelated database; requires operator intervention."""


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.connection = None
        self.lock = FileLock(str(self.path) + '.lock')

    def open(self):
        self.lock.acquire()
        try:
            self.connection = sqlite3.connect(str(self.path), timeout=0.25, isolation_level=None)
            c = self.connection
            c.execute('PRAGMA busy_timeout=250')
            version = c.execute('PRAGMA user_version').fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and not tables:
                # executescript would implicitly commit; execute DDL in one explicit transaction.
                c.execute('BEGIN IMMEDIATE')
                try:
                    for statement in SQL.split(';'):
                        if statement.strip():
                            c.execute(statement)
                    c.execute('COMMIT')
                except BaseException:
                    c.execute('ROLLBACK')
                    raise
            elif version != VERSION or tables != {'core_samples'}:
                raise SchemaError('unsupported_history_schema')
            columns = [r[1] for r in c.execute('PRAGMA table_info(core_samples)')]
            if columns != ['node_id', 'instance_id', 'sequence', 'received_at_ms', 'data_age_ms',
                           'stale_after_ms', 'core_state', 'cpu_percent', 'gpu_percent',
                           'memory_percent', 'disk_percent', 'hottest_c']:
                raise SchemaError('invalid_history_layout')
            if c.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise SchemaError('wal_unavailable')
            c.execute('PRAGMA synchronous=FULL')
            c.execute('PRAGMA wal_autocheckpoint=1000')
            return self
        except BaseException:
            self.close()
            raise

    def insert_batch(self, records):
        c = self.connection
        counts = {'persisted': 0, 'duplicates': 0, 'dropped_regression': 0}
        c.execute('BEGIN IMMEDIATE')
        try:
            for record in records:
                key = (record.node_id, record.instance_id)
                highest = c.execute('SELECT max(sequence) FROM core_samples WHERE node_id=? AND instance_id=?', key).fetchone()[0]
                exists = c.execute('SELECT 1 FROM core_samples WHERE node_id=? AND instance_id=? AND sequence=?', key + (record.sequence,)).fetchone()
                if exists:
                    counts['duplicates'] += 1
                elif highest is not None and record.sequence < highest:
                    counts['dropped_regression'] += 1
                else:
                    c.execute('INSERT INTO core_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', record.values())
                    counts['persisted'] += 1
            c.execute('COMMIT')
            return counts
        except BaseException:
            if c.in_transaction:
                c.execute('ROLLBACK')
            raise

    def prune(self, cutoff_ms, limit=1000):
        c = self.connection
        c.execute('BEGIN IMMEDIATE')
        try:
            count = c.execute('DELETE FROM core_samples WHERE rowid IN '
                              '(SELECT rowid FROM core_samples WHERE received_at_ms < ? LIMIT ?)',
                              (cutoff_ms, limit)).rowcount
            c.execute('COMMIT')
            return count
        except BaseException:
            if c.in_transaction:
                c.execute('ROLLBACK')
            raise

    def close(self):
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        finally:
            self.lock.close()


def read_connection(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.25)
    c.execute('PRAGMA query_only=ON')
    c.execute('PRAGMA busy_timeout=250')
    return c
