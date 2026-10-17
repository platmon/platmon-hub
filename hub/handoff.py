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
        adopted = recover(store, directory)
        if adopted is not None:
            return adopted
        state = store.handoff_state()
        nodes = load_registry(registry) if registry is not None else None
        return _build(store, directory, state, nodes, max_bytes, now_ms)
    finally:
        store.close()


def recover(store, directory):
    """Adopt a segment published before its delivery was recorded; refuse any other unknown file."""
    state = store.handoff_state()
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
        _deliver(store, state['delivered_seq'], last, entry.name, digest)
        return dict(segment=entry.name, from_seq=first - 1, to_seq=last, sha256=digest, adopted=True)
    return None


def read_manifest(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read('manifest.json'))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise HandoffError('unreadable handoff segment: ' + Path(path).name) from exc


def _deliver(store, previous, to_seq, name, digest):
    c = store.connection
    c.execute('BEGIN IMMEDIATE')
    try:
        if c.execute('SELECT delivered_seq FROM handoff_state').fetchone()[0] != previous:
            raise HandoffError('handoff state changed during publication')
        c.execute('UPDATE handoff_state SET delivered_seq=?, last_segment=?, last_digest=?', (to_seq, name, digest))
        c.execute('COMMIT')
    except BaseException:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise


def _build(store, directory, state, nodes, max_bytes, now_ms):
    reader = read_connection(store.path)
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
        total, to_seq = 0, low
        streams = [_rows(reader, table, low, high, envelopes) for table in MEMBERS]
        for seq, member, row in heapq.merge(*streams, key=lambda item: item[0]):
            data = encode(row)
            if total and total + len(data) > max_bytes:
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
        _deliver(store, low, to_seq, name, digest)
        return dict(segment=name, from_seq=low, to_seq=to_seq, sha256=digest, adopted=False,
                    rows=sum(f['rows'] for f in files), uncompressed_bytes=total)
    finally:
        reader.close()
        if temp is not None and temp.exists():
            temp.unlink()
        scratch.cleanup()


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
