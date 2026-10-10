"""Synchronous SQLite operations, owned exclusively by the history writer."""

import sqlite3
import time
import uuid
from pathlib import Path

from ..filelock import FileLock
from .schema import SQL, VERSION, MIGRATION_2
from .projector import ObservationRecord, ContinuityRecord


class SchemaError(Exception):
    """Unsupported or unrelated database; requires operator intervention."""


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.connection = None
        self.session_id = None
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
                self._migrate(SQL)
                version = 1
            elif version not in (1, VERSION):
                raise SchemaError('unsupported_history_schema')
            columns = [r[1] for r in c.execute('PRAGMA table_info(core_samples)')]
            if columns != ['node_id', 'instance_id', 'sequence', 'received_at_ms', 'data_age_ms',
                           'stale_after_ms', 'core_state', 'cpu_percent', 'gpu_percent',
                           'memory_percent', 'disk_percent', 'hottest_c']:
                raise SchemaError('invalid_history_layout')
            if version == 1:
                if tables and tables != {'core_samples'}:
                    raise SchemaError('invalid_history_layout')
                backup_path = self.path.with_name(self.path.name + '.v1-backup-' + uuid.uuid4().hex)
                backup = sqlite3.connect(str(backup_path))
                try:
                    c.backup(backup)
                    if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise SchemaError('backup_verification_failed')
                finally:
                    backup.close()
                self._migrate(MIGRATION_2)
            expected = {'core_samples', 'observations', 'gaps', 'continuity', 'hub_sessions'}
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != expected:
                raise SchemaError('invalid_history_layout')
            if c.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise SchemaError('wal_unavailable')
            c.execute('PRAGMA synchronous=FULL')
            c.execute('PRAGMA wal_autocheckpoint=1000')
            self.session_id = uuid.uuid4().hex
            now = int(time.time() * 1000)
            c.execute('BEGIN IMMEDIATE')
            try:
                for node, scope, instance, previous, reason, gap_id in c.execute('SELECT * FROM continuity').fetchall():
                    if gap_id is not None:
                        c.execute('UPDATE gaps SET end_ms=? WHERE id=?', (max(previous, now), gap_id))
                    gap = self._gap(node, scope, instance, min(previous, now), None, 'unobserved')
                    c.execute('UPDATE continuity SET reason=?, gap_id=? WHERE node_id=? AND scope=?',
                              ('unobserved', gap, node, scope))
                c.execute('INSERT INTO hub_sessions VALUES (?,?,?,NULL)', (self.session_id, now, now))
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
            return self
        except BaseException:
            self.close()
            raise

    def _migrate(self, script):
        c = self.connection
        c.execute('BEGIN IMMEDIATE')
        try:
            for statement in script.split(';'):
                if statement.strip():
                    c.execute(statement)
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise

    def _gap(self, node, scope, instance, start, end, reason):
        return self.connection.execute('INSERT INTO gaps (node_id,scope,instance_id,start_ms,end_ms,reason) '
                                       'VALUES (?,?,?,?,?,?)', (node, scope, instance, start, end, reason)).lastrowid

    def _continuity(self, record):
        c = self.connection
        old = c.execute('SELECT instance_id,received_at_ms,reason,gap_id FROM continuity WHERE node_id=? AND scope=?',
                        (record.node_id, record.scope)).fetchone()
        gap_id = None
        if old is not None:
            instance, previous, reason, gap_id = old
            if gap_id is not None and (reason != record.reason or instance != record.instance_id):
                c.execute('UPDATE gaps SET end_ms=? WHERE id=?', (max(previous, record.received_at_ms), gap_id))
                gap_id = None
            boundary = None
            if instance is not None and record.instance_id is not None and instance != record.instance_id:
                boundary = 'agent_restart'
            elif record.received_at_ms < previous:
                boundary = 'clock_discontinuity'
            elif record.received_at_ms - previous > record.silence_ms and reason is None:
                boundary = 'unobserved'
            if boundary:
                self._gap(record.node_id, record.scope, record.instance_id, min(previous, record.received_at_ms),
                          max(previous, record.received_at_ms), boundary)
        if record.reason is not None and gap_id is None:
            gap_id = self._gap(record.node_id, record.scope, record.instance_id, record.received_at_ms, None, record.reason)
        c.execute('INSERT INTO continuity VALUES (?,?,?,?,?,?) ON CONFLICT(node_id,scope) DO UPDATE SET '
                  'instance_id=excluded.instance_id, received_at_ms=excluded.received_at_ms, '
                  'reason=excluded.reason, gap_id=excluded.gap_id',
                  (record.node_id, record.scope, record.instance_id, record.received_at_ms, record.reason, gap_id))

    def insert_batch(self, records):
        c = self.connection
        counts = {'persisted': 0, 'duplicates': 0, 'dropped_regression': 0}
        c.execute('BEGIN IMMEDIATE')
        try:
            for record in records:
                if isinstance(record, ContinuityRecord):
                    self._continuity(record)
                    counts['persisted'] += 1
                    continue
                if isinstance(record, ObservationRecord):
                    key = (record.node_id, record.instance_id, record.group_name)
                    highest = c.execute('SELECT max(observation_id) FROM observations WHERE node_id=? AND instance_id=? AND group_name=?', key).fetchone()[0]
                    exists = c.execute('SELECT 1 FROM observations WHERE node_id=? AND instance_id=? AND group_name=? AND observation_id=?', key + (record.observation_id,)).fetchone()
                    if exists:
                        counts['duplicates'] += 1
                    elif highest is not None and record.observation_id < highest:
                        counts['dropped_regression'] += 1
                    else:
                        c.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)', record.values())
                        counts['persisted'] += 1
                    continue
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
            c.execute('UPDATE hub_sessions SET last_seen_ms=? WHERE session_id=?', (int(time.time() * 1000), self.session_id))
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
            count += c.execute('DELETE FROM observations WHERE rowid IN '
                               '(SELECT rowid FROM observations WHERE received_at_ms < ? LIMIT ?)',
                               (cutoff_ms, limit)).rowcount
            c.execute('DELETE FROM gaps WHERE id IN (SELECT id FROM gaps WHERE end_ms < ? LIMIT ?)', (cutoff_ms, limit))
            c.execute('UPDATE gaps SET end_ms=? WHERE id IN (SELECT gap_id FROM continuity WHERE received_at_ms < ? LIMIT ?)', (cutoff_ms, cutoff_ms, limit))
            c.execute('DELETE FROM continuity WHERE rowid IN (SELECT rowid FROM continuity WHERE received_at_ms < ? LIMIT ?)', (cutoff_ms, limit))
            c.execute('DELETE FROM hub_sessions WHERE session_id IN (SELECT session_id FROM hub_sessions WHERE last_seen_ms < ? AND session_id != ? LIMIT ?)', (cutoff_ms, self.session_id, limit))
            c.execute('COMMIT')
            return count
        except BaseException:
            if c.in_transaction:
                c.execute('ROLLBACK')
            raise

    def close(self):
        try:
            if self.connection is not None:
                if self.session_id is not None:
                    try:
                        self.connection.execute('UPDATE hub_sessions SET closed_at_ms=? WHERE session_id=?',
                                                (int(time.time() * 1000), self.session_id))
                    except sqlite3.Error:
                        pass
                self.connection.close()
                self.connection = None
        finally:
            self.lock.close()


def read_connection(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.25)
    c.execute('PRAGMA query_only=ON')
    c.execute('PRAGMA busy_timeout=250')
    return c
