"""Downstream handoff segments (ADR-0004): build, publish and record delivery.

A segment holds every row version with from_seq < handoff_seq <= to_seq, read from one
snapshot, and names its predecessor's SHA-256 so a consumer can prove the chain is complete.
"""

import argparse
import hashlib
import heapq
import json
import os
import re
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path

from . import __version__
from .history.codec import decode
from .history.schema import VERSION
from .history.store import HANDOFF_TABLES, HistoryStore, SchemaError, read_connection
from .raw_export import PAYLOAD_SCHEMAS, TIMESTAMPS, UNITS
from .registry import load_registry

FORMAT, FORMAT_VERSION = 'platmon-hub-handoff', 1
MAX_SEGMENT_BYTES = 256 * 1024**2
WIDTH = 20  # zero-padded sequence numbers keep segment names in chain order
NAME = re.compile(r'^(\d{%d})-(\d{%d})\.zip$' % (WIDTH, WIDTH))
MEMBERS = dict(raw_publications='raw_publications.jsonl', replay_losses='replay_losses.jsonl',
               gaps='gaps.jsonl', events='events.jsonl', hub_sessions='hub_sessions.jsonl',
               handoff_losses='handoff_losses.jsonl')
RAW = ('SELECT p.handoff_seq, p.node_id, p.instance_id, p.stream, p.publication_id, p.received_at_ms, '
       'p.reported_started_at, p.reported_completed_at, p.gap_before, p.data_json, p.payload_zlib, d.content, '
       'e.digest, e.envelope_json FROM raw_publications p JOIN raw_envelopes e ON e.envelope_id=p.envelope_id '
       'LEFT JOIN raw_dictionaries d ON d.dictionary_id=p.dictionary_id '
       'WHERE p.handoff_seq > ? AND p.handoff_seq <= ? ORDER BY p.handoff_seq')


class HandoffError(Exception):
    pass


def segment_name(from_seq, to_seq):
    return '%0*d-%0*d.zip' % (WIDTH, from_seq + 1, WIDTH, to_seq)


def encode(value):
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n').encode()


def digest_file(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def _rows(connection, table, low, high, envelopes):
    """(handoff_seq, member name, row dict) in sequence order for one table."""
    if table == 'raw_publications':
        for (seq, node, instance, stream, pid, received, started, completed, gap, data_json, blob, dictionary,
             envelope_digest, envelope_json) in connection.execute(RAW, (low, high)):
            try:
                payload = decode(data_json, blob, dictionary)
            except (ValueError, UnicodeDecodeError, zlib.error) as exc:
                raise HandoffError('raw payload cannot be decoded') from exc
            envelopes.setdefault(envelope_digest, envelope_json)
            yield seq, MEMBERS[table], dict(
                handoff_seq=seq, node_id=node, instance_id=instance, stream=stream, publication_id=pid,
                received_at_ms=received, reported_started_at=started, reported_completed_at=completed,
                gap_before=bool(gap), encoded_bytes=len(payload.encode()) + 256, envelope_digest=envelope_digest,
                data=json.loads(payload))
        return
    query = connection.execute('SELECT * FROM ' + table + ' WHERE handoff_seq > ? AND handoff_seq <= ? '
                               'ORDER BY handoff_seq', (low, high))
    names = [column[0] for column in query.description]
    for values in query:
        row = dict(zip(names, values))
        yield row['handoff_seq'], MEMBERS[table], row


def build_segment(database, spool, *, registry=None, max_bytes=MAX_SEGMENT_BYTES, now_ms=None):
    """Publish the next segment, or return None when nothing new is committed.

    Requires exclusive access (no Hub running on the database); a running Hub uses its writer.
    """
    store = HistoryStore(database).open_for_maintenance()
    try:
        state = store.handoff_state()
        if not state['enabled']:
            raise HandoffError('handoff is not enabled')
        directory = Path(spool).resolve() / state['store_id']
        if not Path(spool).is_dir():
            raise HandoffError('spool directory does not exist')
        directory.mkdir(exist_ok=True)
        deliver = lambda *args: store.record_delivery(*args)
        adopted = recover(state, directory, deliver)
        if adopted is not None:
            return adopted
        nodes = load_registry(registry) if registry is not None else None
        return _build(store.path, directory, store.handoff_state(), nodes, max_bytes, now_ms, deliver)
    finally:
        store.close()


def segments(directory):
    """(first sequence, last sequence, path) of published segments in chain order."""
    found = []
    for entry in sorted(directory.iterdir()):
        match = NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), int(match.group(2)), entry))
    return found


def recover(state, directory, deliver):
    """Adopt a segment published before its delivery was recorded; refuse any other unknown file."""
    for entry in sorted(directory.iterdir()):
        match = NAME.match(entry.name)
        if entry.name.startswith('.') or entry.is_dir():
            continue
        if match is None:
            raise HandoffError('unexpected file in handoff spool: ' + entry.name)
        first, last = int(match.group(1)), int(match.group(2))
        if last <= state['delivered_seq']:
            continue
        if first != state['delivered_seq'] + 1:
            raise HandoffError('handoff spool does not continue the recorded chain: ' + entry.name)
        manifest = read_manifest(entry)
        if (manifest.get('hub_store_id') != state['store_id'] or manifest.get('previous_sha256') != state['last_digest'] or
                (manifest.get('from_seq'), manifest.get('to_seq')) != (first - 1, last)):
            raise HandoffError('handoff spool segment does not match the recorded chain: ' + entry.name)
        digest = digest_file(entry)
        deliver(state['delivered_seq'], last, entry.name, digest)
        return dict(segment=entry.name, from_seq=first - 1, to_seq=last, sha256=digest, adopted=True, complete=False)
    return None


def read_manifest(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read('manifest.json'))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise HandoffError('unreadable handoff segment: ' + Path(path).name) from exc


def _build(path, directory, state, nodes, max_bytes, now_ms, deliver):
    reader = read_connection(path)
    scratch = tempfile.TemporaryDirectory(dir=str(directory), prefix='.platmon-handoff-')
    temp = None
    try:
        reader.execute('BEGIN')
        low = state['delivered_seq']
        high = reader.execute('SELECT next_seq FROM handoff_state').fetchone()[0]
        if high <= low:
            return None
        envelopes, counts, sizes, handles = {}, {}, {}, {}
        hashers = {name: hashlib.sha256() for name in MEMBERS.values()}
        first_ms = last_ms = None
        total, to_seq, complete = 0, low, True
        streams = [_rows(reader, table, low, high, envelopes) for table in MEMBERS]
        for seq, member, row in heapq.merge(*streams, key=lambda item: item[0]):
            data = encode(row)
            if total and total + len(data) > max_bytes:
                complete = False
                break
            if member not in handles:
                handles[member] = open(os.path.join(scratch.name, member), 'wb')
            handles[member].write(data)
            hashers[member].update(data)
            counts[member] = counts.get(member, 0) + 1
            sizes[member] = sizes.get(member, 0) + len(data)
            total += len(data)
            to_seq = seq
            received = row.get('received_at_ms', row.get('last_seen_ms', row.get('start_ms')))
            if isinstance(received, int):
                first_ms = received if first_ms is None else min(first_ms, received)
                last_ms = received if last_ms is None else max(last_ms, received)
        for handle in handles.values():
            handle.close()
        reader.rollback()
        # Only the envelopes of rows that made it into this segment.
        with open(os.path.join(scratch.name, 'raw_envelopes.jsonl'), 'wb') as handle:
            referenced = set()
            if 'raw_publications.jsonl' in handles:
                with open(os.path.join(scratch.name, 'raw_publications.jsonl'), 'rb') as rows:
                    referenced = {json.loads(line)['envelope_digest'] for line in rows}
            hasher, rows_written, size = hashlib.sha256(), 0, 0
            for envelope_digest in sorted(referenced):
                data = encode(dict(digest=envelope_digest, envelope=json.loads(envelopes[envelope_digest])))
                handle.write(data)
                hasher.update(data)
                rows_written += 1
                size += len(data)
        files = []
        for member in list(MEMBERS.values()) + ['raw_envelopes.jsonl']:
            if member == 'raw_envelopes.jsonl':
                files.append(dict(name=member, rows=rows_written, bytes=size, sha256=hasher.hexdigest()))
            else:
                files.append(dict(name=member, rows=counts.get(member, 0), bytes=sizes.get(member, 0),
                                  sha256=hashers[member].hexdigest()))
        node_rows = [dict(node_id=n.node_id, enabled=n.enabled, capture=n.capture) for n in nodes or ()]
        node_data = b''.join(encode(row) for row in node_rows)
        files.append(dict(name='nodes.jsonl', rows=len(node_rows), bytes=len(node_data),
                          sha256=hashlib.sha256(node_data).hexdigest()))
        name = segment_name(low, to_seq)
        manifest = dict(format=FORMAT, format_version=FORMAT_VERSION, hub_store_id=state['store_id'], segment=name,
                        from_seq=low, to_seq=to_seq, previous_segment=state['last_segment'],
                        previous_sha256=state['last_digest'], hub_version=__version__, database_schema_version=VERSION,
                        start_mode=state['start_mode'], payload_schemas=PAYLOAD_SCHEMAS, timestamps=TIMESTAMPS,
                        units=UNITS, registry_snapshot=nodes is not None,
                        received_bounds_ms=dict(first=first_ms, last=last_ms),
                        created_at_ms=int(time.time() * 1000) if now_ms is None else now_ms, files=files)
        with tempfile.NamedTemporaryFile(dir=str(directory), prefix='.platmon-handoff-', suffix='.zip',
                                         delete=False) as handle:
            temp = Path(handle.name)
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for member in [f['name'] for f in files if f['name'] != 'nodes.jsonl']:
                source = os.path.join(scratch.name, member)
                if os.path.exists(source):
                    archive.write(source, member)
                else:
                    archive.writestr(member, b'')
            archive.writestr('nodes.jsonl', node_data)
            archive.writestr('manifest.json', encode(manifest))
        target = directory / name
        # A hard link publishes the complete file exclusively; an existing name is never replaced.
        os.link(str(temp), str(target))
        digest = digest_file(target)
        deliver(low, to_seq, name, digest)
        return dict(segment=name, from_seq=low, to_seq=to_seq, sha256=digest, adopted=False, complete=complete,
                    rows=sum(f['rows'] for f in files), uncompressed_bytes=total,
                    received_bounds_ms=dict(first=first_ms, last=last_ms))
    finally:
        reader.close()
        if temp is not None and temp.exists():
            temp.unlink()
        scratch.cleanup()


def validate_spool(spool):
    """Startup checks for a running Hub: an existing, writable directory with hard links."""
    path = Path(spool)
    if not path.is_dir():
        raise HandoffError('handoff spool must be an existing directory')
    try:
        with tempfile.NamedTemporaryFile(dir=str(path), prefix='.platmon-handoff-check-', delete=False) as handle:
            probe = Path(handle.name)
        link = probe.with_name(probe.name + '.link')
        try:
            os.link(str(probe), str(link))
            link.unlink()
        finally:
            probe.unlink()
    except OSError as exc:
        raise HandoffError('handoff spool must be writable and support hard links') from exc


class HandoffScheduler:
    """Builds segments inside a running Hub; every state change goes through its history writer."""

    def __init__(self, writer, spool, *, quota_bytes, retention_ms, data_retention_ms, start='now',
                 registry=None, interval_s=900.0, max_bytes=MAX_SEGMENT_BYTES, tick_s=10.0, timeout_s=60.0,
                 clock=time.monotonic, wall=time.time):
        self.writer, self.spool, self.registry = writer, Path(spool).resolve(), registry
        self.quota_bytes, self.retention_ms, self.data_retention_ms = quota_bytes, retention_ms, data_retention_ms
        self.start_mode, self.interval_s, self.max_bytes = start, interval_s, max_bytes
        self.tick_s, self.timeout_s, self.clock, self.wall = tick_s, timeout_s, clock, wall
        self.directory = None
        self._digests, self._bounds = {}, {}
        self._last_build = None
        self._more = False
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._stats = dict(enabled=True, store_id=None, segments_built=0, segments_adopted=0, segments_deleted=0,
                           segments_lost=0, rejected_acks=0, spool_bytes=0, unacknowledged_segments=0,
                           unacknowledged_bytes=0, spool_full=False, backfill_pending=False, last_segment=None,
                           last_build_utc_ms=None, last_error=None, spool_quota_bytes=quota_bytes,
                           spool_retention_ms=retention_ms, data_retention_ms=data_retention_ms, next_seq=None,
                           delivered_seq=None, acked_seq=None, unacknowledged_raw_bytes=None, raw_quota_bytes=None,
                           oldest_unacknowledged_received_ms=None, losses=[], warnings=[])

    def _call(self, method, *args):
        return self.writer.handoff(method, *args).result(self.timeout_s)

    def _deliver(self, *args):
        return self._call('record_delivery', *args)

    def start(self):
        state = self._call('enable_handoff', self.start_mode, int(self.wall() * 1000))
        self.directory = self.spool / state['store_id']
        self.directory.mkdir(exist_ok=True)
        self._last_build = self.clock()
        with self._lock:
            self._stats['store_id'] = state['store_id']
        self._thread = threading.Thread(target=self._run, name='hub-handoff', daemon=True)
        self._thread.start()
        return self

    def close(self, final=True):
        """Stop the thread; a clean shutdown publishes what is pending as a last segment."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.timeout_s)
        if final and self.directory is not None:
            self.tick(force=True)

    def _run(self):
        while not self._stop.wait(self.tick_s):
            self.tick()

    def snapshot(self):
        with self._lock:
            return dict(self._stats)

    def tick(self, force=False):
        try:
            state = self._call('handoff_state')
            if recover(state, self.directory, self._deliver) is not None:
                with self._lock:
                    self._stats['segments_adopted'] += 1
                state = self._call('handoff_state')
            state = self._acknowledge(state)
            state = self._expire(state)
            backfill = state['backfill_bound_ms'] is not None
            if backfill:
                self._call('backfill_handoff', 1000)
                backfill = self._call('handoff_state')['backfill_bound_ms'] is not None
            due = force or self._more or self.clock() - self._last_build >= self.interval_s
            built = None
            if due and not self.snapshot()['spool_full']:
                nodes = None
                if self.registry is not None:
                    try:
                        nodes = load_registry(self.registry)
                    except (OSError, ValueError, UnicodeError):
                        nodes = None
                built = _build(self.writer.path, self.directory, self._call('handoff_state'), nodes,
                               self.max_bytes, None, self._deliver)
                self._last_build = self.clock()
                self._more = built is not None and not built['complete']
            measured = self._measure()
            with self._lock:
                self._stats.update(measured)
                self._stats['backfill_pending'] = backfill
                if built is not None:
                    self._stats['segments_built'] += 1
                    self._stats['last_segment'] = built['segment']
                    self._stats['last_build_utc_ms'] = int(self.wall() * 1000)
                    self._bounds[built['segment']] = built['received_bounds_ms']
                    self._digests[built['segment']] = built['sha256']
                self._stats['last_error'] = None
                self._stats['warnings'] = self._warnings(self._stats)
            return built
        except Exception as exc:  # reported and retried on the next tick; collection continues
            with self._lock:
                self._stats['last_error'] = str(exc) if isinstance(exc, (HandoffError, ValueError)) else type(exc).__name__
                self._stats['warnings'] = self._warnings(self._stats)
            return None

    def _measure(self):
        """Read-only figures for diagnostics: sequence positions, unacknowledged data and losses."""
        reader = read_connection(self.writer.path)
        try:
            reader.execute('BEGIN')
            next_seq, delivered, acked = reader.execute('SELECT next_seq, delivered_seq, acked_seq FROM handoff_state').fetchone()
            raw_bytes = reader.execute('SELECT coalesce(sum(encoded_bytes),0) FROM raw_publications WHERE handoff_seq > ?',
                                       (acked,)).fetchone()[0]
            oldest = [reader.execute('SELECT min(' + column + ') FROM ' + table + ' WHERE handoff_seq > ?', (acked,)).fetchone()[0]
                      for table, column in HANDOFF_TABLES.items()]
            losses = [dict(table=table, reason=reason, records=records, rows=rows) for table, reason, records, rows in reader.execute(
                "SELECT CASE WHEN table_name LIKE 'segment:%' THEN 'segment' ELSE table_name END AS kind, reason, "
                'count(*), sum(rows) FROM handoff_losses GROUP BY kind, reason ORDER BY kind, reason')]
            return dict(next_seq=next_seq, delivered_seq=delivered, acked_seq=acked, unacknowledged_raw_bytes=raw_bytes,
                        raw_quota_bytes=getattr(self.writer, 'raw_quota_bytes', None),
                        oldest_unacknowledged_received_ms=min((v for v in oldest if v is not None), default=None),
                        losses=losses)
        finally:
            reader.close()

    def _warnings(self, stats):
        """Conditions an operator should act on before data is lost or production stops."""
        warnings = []
        if stats['spool_full']:
            warnings.append('spool_full')
        elif stats['spool_quota_bytes'] and stats['spool_bytes'] >= 0.8 * stats['spool_quota_bytes']:
            warnings.append('spool_near_quota')
        quota = stats['raw_quota_bytes']
        if quota and stats['unacknowledged_raw_bytes'] is not None and stats['unacknowledged_raw_bytes'] >= 0.8 * quota:
            warnings.append('unacknowledged_raw_near_quota')
        oldest = stats['oldest_unacknowledged_received_ms']
        if oldest is not None and self.wall() * 1000 - oldest >= 0.8 * self.data_retention_ms:
            warnings.append('unacknowledged_near_retention')
        if stats['rejected_acks']:
            warnings.append('rejected_acknowledgement')
        if stats['last_error']:
            warnings.append('error')
        return warnings

    def _digest(self, path):
        if path.name not in self._digests:
            self._digests[path.name] = digest_file(path)
        return self._digests[path.name]

    def _acknowledge(self, state):
        """Advance acknowledgement over the longest prefix of segments with a matching .ack file."""
        acks = self.directory / 'acks'
        acked, rejected = state['acked_seq'], 0
        for first, last, path in segments(self.directory):
            if last <= acked:
                continue
            if first != acked + 1:
                break
            try:
                content = (acks / (path.name + '.ack')).read_text(encoding='ascii').strip()
            except (OSError, UnicodeError):
                break
            if content != self._digest(path):
                rejected += 1  # partial or tampered; checked again on the next tick
                break
            acked = last
        with self._lock:
            self._stats['rejected_acks'] = rejected
        if acked > state['acked_seq']:
            state = self._call('record_acknowledged', acked)
        return state

    def _expire(self, state):
        """Spool retention for acknowledged segments; data retention for unacknowledged ones, as a loss."""
        now_ms = self.wall() * 1000
        total = unacked = unacked_bytes = deleted = lost = 0
        for first, last, path in segments(self.directory):
            size = path.stat().st_size
            if last <= state['acked_seq']:
                if now_ms - path.stat().st_mtime * 1000 > self.retention_ms:
                    self._remove(path)
                    deleted += 1
                    continue
            else:
                bounds = self._bounds.get(path.name)
                if bounds is None:
                    manifest = read_manifest(path)
                    bounds = self._bounds[path.name] = dict(manifest['received_bounds_ms'],
                                                            rows=sum(f['rows'] for f in manifest['files']))
                newest = bounds.get('last')
                if first == state['acked_seq'] + 1 and newest is not None and newest < now_ms - self.data_retention_ms:
                    if 'rows' not in bounds:
                        bounds['rows'] = sum(f['rows'] for f in read_manifest(path)['files'])
                    state = self._call('record_segment_loss', path.name, first - 1, last, bounds['rows'],
                                       bounds.get('first'), newest)
                    self._remove(path)
                    lost += 1
                    continue
                unacked += 1
                unacked_bytes += size
            total += size
        with self._lock:
            self._stats.update(spool_bytes=total, unacknowledged_segments=unacked, unacknowledged_bytes=unacked_bytes,
                               spool_full=total >= self.quota_bytes)
            self._stats['segments_deleted'] += deleted
            self._stats['segments_lost'] += lost
        return state

    def _remove(self, path):
        for target in (path, self.directory / 'acks' / (path.name + '.ack')):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        self._digests.pop(path.name, None)
        self._bounds.pop(path.name, None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='platmon-hub handoff',
                                     description='Offline handoff maintenance; run while no Hub uses the database')
    parser.add_argument('--db', required=True, help='history database')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status')
    enable = commands.add_parser('enable')
    enable.add_argument('--start', choices=('now', 'existing'), required=True)
    commands.add_parser('disable')
    build = commands.add_parser('build', help='publish at most one new segment')
    build.add_argument('--spool', required=True, help='existing spool directory')
    build.add_argument('--nodes', help='registry whose node_id, enabled and capture go into nodes.jsonl')
    build.add_argument('--max-mib', type=int, default=MAX_SEGMENT_BYTES // 1024**2)
    args = parser.parse_args(argv)
    try:
        if args.action == 'build':
            if not 1 <= args.max_mib <= 1024:
                raise HandoffError('--max-mib must be from 1 to 1024')
            result = build_segment(args.db, args.spool, registry=args.nodes, max_bytes=args.max_mib * 1024**2)
            print(json.dumps(result if result is not None else dict(segment=None)))
            return
        store = HistoryStore(args.db).open_for_maintenance()
        try:
            if args.action == 'enable':
                state = store.enable_handoff(args.start, int(time.time() * 1000))
            elif args.action == 'disable':
                store.disable_handoff()
                state = store.handoff_state()
            else:
                state = store.handoff_state()
            pending = sum(store.connection.execute('SELECT count(*) FROM ' + table + ' WHERE handoff_seq > ?',
                                                   (state['delivered_seq'],)).fetchone()[0]
                          for table in list(HANDOFF_TABLES) + ['handoff_losses'])
            print(json.dumps(dict(state, undelivered_versions=pending)))
        finally:
            store.close()
    except (HandoffError, SchemaError, OSError, ValueError) as exc:
        parser.exit(2, 'Handoff failed: ' + (str(exc) if isinstance(exc, (HandoffError, SchemaError)) else
                                            type(exc).__name__) + '\n')
