"""Bounded read-only event history with stable pagination."""
import sqlite3
import time
import uuid
from urllib.parse import parse_qs

from .projector import EVENT_TYPES
from .query import QueryError
from .schema import VERSION
from .store import read_connection


def query_events(path, query):
    try:
        # Python 3.9 treats an empty strict query as an invalid field.
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=6) if query else {}
        if set(params) - {'from_ms', 'to_ms', 'node_id', 'type', 'limit', 'cursor'} or any(len(v) != 1 for v in params.values()):
            raise ValueError()
        args = {key: value[0] for key, value in params.items()}
        end = int(args.get('to_ms', int(time.time()*1000)+1))
        start = int(args.get('from_ms', max(0, end-86400000)))
        limit = int(args.get('limit', 100))
        if not (0 <= start < end < 2**63 and end-start <= 30*86400000 and 1 <= limit <= 500):
            raise ValueError()
        clauses, values = ['received_at_ms>=?', 'received_at_ms<?'], [start, end]
        if 'node_id' in args:
            if str(uuid.UUID(args['node_id'])) != args['node_id']: raise ValueError()
            clauses.append('node_id=?'); values.append(args['node_id'])
        if 'type' in args:
            if args['type'] not in EVENT_TYPES: raise ValueError()
            clauses.append('event_type=?'); values.append(args['type'])
        if 'cursor' in args:
            stamp, identity = args['cursor'].split(':', 1)
            stamp = int(stamp)
            if not 0 <= stamp < 2**63 or str(uuid.UUID(identity)) != identity: raise ValueError()
            clauses.append('(received_at_ms < ? OR (received_at_ms = ? AND event_id < ?))')
            values.extend((stamp, stamp, identity))
    except (ValueError, KeyError):
        raise QueryError('invalid_event_query') from None
    connection = None
    deadline = time.monotonic()+2
    try:
        connection = read_connection(path)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            raise QueryError('history_schema_unavailable', 503)
        keys = ('node_id', 'event_type', 'received_at_ms', 'previous_state', 'current_state', 'reason_code', 'event_id')
        rows = [dict(zip(keys, row)) for row in connection.execute('SELECT * FROM events WHERE ' + ' AND '.join(clauses) +
                ' ORDER BY received_at_ms DESC,event_id DESC LIMIT ?', values + [limit+1])]
        cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            cursor = str(rows[-1]['received_at_ms']) + ':' + rows[-1]['event_id']
        return {'schema_version': 1, 'events': rows, 'next_cursor': cursor, 'from_ms': start, 'to_ms': end}
    except sqlite3.Error as exc:
        raise QueryError('events_unavailable', 503) from exc
    finally:
        if connection is not None: connection.close()
