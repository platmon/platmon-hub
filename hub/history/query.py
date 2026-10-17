"""Bounded, read-only metric queries; never connect unrelated generations."""

import math
import sqlite3
import time
from urllib.parse import parse_qs

from .projector import LOAD_COLUMNS
from .schema import VERSION
from .store import read_connection

METRICS = ('cpu_percent', 'gpu_percent', 'memory_percent', 'disk_percent', 'hottest_c', 'npu_percent') + LOAD_COLUMNS
MAX_SPAN_MS = 7 * 86400000
MAX_POINTS = 1200


class QueryError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def parameters(query):
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        if set(values) - {'metric', 'from_ms', 'to_ms', 'max_points'} or any(len(v) != 1 for v in values.values()):
            raise ValueError()
        metric = values['metric'][0]
        if metric not in METRICS:
            raise ValueError()
        start, end = int(values['from_ms'][0]), int(values['to_ms'][0])
        maximum = int(values.get('max_points', ['1200'])[0])
        if not (0 <= start < end < 2**63 and end - start <= MAX_SPAN_MS and 1 <= maximum <= MAX_POINTS):
            raise ValueError()
        return metric, start, end, maximum
    except (ValueError, KeyError):
        raise QueryError('invalid_history_query') from None


def query_history(path, node_id, metric, start, end, maximum, *, budget_seconds=2):
    if metric not in METRICS or not (0 <= start < end < 2**63 and end-start <= MAX_SPAN_MS and 1 <= maximum <= MAX_POINTS):
        raise QueryError('invalid_history_query')
    deadline = time.monotonic() + budget_seconds
    connection = None
    try:
        connection = read_connection(path)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            raise QueryError('history_schema_unavailable', 503)
        gaps = [dict(zip(('start_ms', 'end_ms', 'reason', 'instance_id'), row)) for row in connection.execute(
            "SELECT start_ms,end_ms,reason,instance_id FROM gaps WHERE node_id=? AND scope='core' "
            'AND start_ms < ? AND (end_ms IS NULL OR end_ms >= ?) ORDER BY start_ms LIMIT ?',
            (node_id, end, start, maximum + 1))]
        if len(gaps) > maximum:
            raise QueryError('history_range_too_dense', 422)
        # Treat both edges of every evidence interval as continuity boundaries.
        edges = sorted({max(start, g['start_ms']) for g in gaps} |
                       {g['end_ms'] for g in gaps if g['end_ms'] is not None and start <= g['end_ms'] < end})
        edge_index = 0
        segment = 0
        previous = None
        bins = []
        bucket_width = max(1, math.ceil((end-start) / maximum))
        current_key = None
        for timestamp, instance, sequence, value in connection.execute(
                'SELECT received_at_ms,instance_id,sequence,' + metric +
                ' FROM core_samples WHERE node_id=? AND received_at_ms>=? AND received_at_ms<? '
                'ORDER BY received_at_ms,instance_id,sequence', (node_id, start, end)):
            if time.monotonic() >= deadline:
                raise QueryError('history_query_timeout', 503)
            boundary = False
            while edge_index < len(edges) and edges[edge_index] <= timestamp:
                boundary = True
                edge_index += 1
            # Silence, nulls and restarts all break the plotted line.
            if previous is not None and (boundary or previous[1] != instance or
                                         timestamp - previous[0] > 15000 or
                                         (previous[2] is None) != (value is None)):
                segment += 1
            key = ((timestamp-start)//bucket_width, segment)
            if key != current_key:
                if len(bins) >= maximum:
                    raise QueryError('history_range_too_dense', 422)
                bins.append(dict(timestamp_ms=timestamp, instance_id=instance, segment=segment,
                                 average=None, minimum=None, maximum=None, count=0))
                current_key = key
            point = bins[-1]
            if value is not None:
                count = point['count']
                # Incremental mean avoids an unbounded list/sum of observations.
                point['average'] = value if not count else point['average'] * (count/(count+1)) + value/(count+1)
                point['minimum'] = value if not count else min(point['minimum'], value)
                point['maximum'] = value if not count else max(point['maximum'], value)
                point['count'] += 1
            previous = (timestamp, instance, value)
        return dict(schema_version=1, node_id=node_id, metric=metric, from_ms=start, to_ms=end,
                    max_points=maximum, points=bins, gaps=gaps,
                    timestamp_meaning='hub_receipt_utc_ms', missing='null')
    except sqlite3.Error as exc:
        code = 'history_query_timeout' if time.monotonic() >= deadline else 'history_unavailable'
        raise QueryError(code, 503) from exc
    finally:
        if connection is not None:
            connection.close()
