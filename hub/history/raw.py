"""Immutable replay pages; validation does not invoke live acceptance rules."""

import json
import math
import uuid
from dataclasses import dataclass

from .. import protocol

STREAMS = ('core', 'storage', 'wifi', 'battery', 'probe')
MAX_ID = 2**63 - 1
MAX_PAGE_BYTES = 2 * 1024**2


def integer(value, low=0, high=MAX_ID):
    return type(value) is int and low <= value <= high


def instance(value):
    return isinstance(value, str) and 1 <= len(value.encode('utf-8')) <= 128


def compact(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class RawPage:
    node_id: str
    instance_id: str
    stream: str
    after: int
    next_after: int
    received_at_ms: int
    envelope_json: str
    records: tuple  # (id, payload JSON, gap_before, reported start, reported end)
    losses: tuple
    previous_instance: object = None
    previous_after: int = 0

    @property
    def encoded_bytes(self):
        # Includes encoded envelope, rows and queue bookkeeping reservation.
        return len(self.envelope_json.encode()) + sum(len(r[1].encode()) + 256 for r in self.records) + len(self.losses)*128 + 1024


def validate_page(document, node_id, expected_instance, stream, after, received_at_ms,
                  previous_instance=None, previous_after=0):
    """Reject the whole page unless records + loss ranges exactly cover its advance."""
    def require(condition):
        if not condition:
            raise protocol.ProtocolFailure('invalid replay page')
    try:
        require(str(uuid.UUID(node_id)) == node_id and stream in STREAMS and
                instance(expected_instance) and integer(after) and integer(received_at_ms) and
                integer(previous_after) and (previous_instance is None or instance(previous_instance)))
        protocol._schema(document, 1)
        schema = 1 if stream == 'core' else 2
        if type(document.get('data_schema_version')) is not int or document['data_schema_version'] != schema:
            raise protocol.ContractFailure('unsupported replay data schema')
        require(document.get('instance_id') == expected_instance and document.get('stream') == stream and
                document.get('historical') is True and isinstance(document.get('clock'), dict))
        for key in ('retention_s', 'max_bytes', 'max_records', 'max_record_bytes', 'max_response_bytes'):
            require(finite(document.get(key)) and document[key] > 0)
        latest, cursor, oldest = document.get('latest_id'), document.get('next_after'), document.get('oldest_id')
        require(integer(latest) and integer(cursor, after, latest) and
                (oldest is None or integer(oldest, 1, latest)) and type(document.get('has_more')) is bool)
        records, losses = document.get('records'), document.get('missing_ranges')
        require(isinstance(records, list) and len(records) <= 100 and isinstance(losses, list) and len(losses) <= 101)
        require(not records or (oldest is not None and isinstance(records[0], dict) and integer(records[0].get('id'), oldest)))
        if document['has_more']:
            require(bool(records) and cursor < latest and records[-1].get('id') == cursor)
        else:
            require(cursor == latest)
        rows, spans, prior = [], [], after
        for record in records:
            require(isinstance(record, dict) and integer(record.get('id'), prior+1, cursor) and
                    type(record.get('gap_before')) is bool)
            pid, data = record['id'], record.get('data')
            require(isinstance(data, dict))
            if stream == 'core':
                protocol.stats(data, expected_instance)
                timing = data['sample']
                require(timing['sequence'] == pid)
                threshold = timing['stale_after_ms']
            else:
                protocol.observations({'schema_version': 2, 'instance_id': expected_instance, 'groups': {stream: data}}, expected_instance)
                timing = data.get('observation')
                require(isinstance(timing, dict) and timing.get('id') == pid and
                        isinstance(data.get('collector'), dict) and data['collector'].get('state') in ('ok', 'partial', 'error', 'unavailable') and
                        data.get('collecting_for_ms') is None and 'data' in data)
                threshold = data['stale_after_ms']
            require(threshold > 0 and type(timing.get('stale')) is bool and
                    timing['stale'] == (timing['data_age_ms'] > threshold))
            for key in ('started_at', 'completed_at', 'duration_ms', 'age_ms', 'data_age_ms'):
                require(finite(timing.get(key)) and (key in ('started_at', 'completed_at') or timing[key] >= 0))
            for key in ('cycle_age_ms', 'interval_ms'):
                if key in timing:
                    require(finite(timing[key]) and timing[key] >= 0)
            payload = compact(data)
            require(len(payload.encode()) <= 1024**2)
            rows.append((pid, payload, record['gap_before'], timing['started_at'], timing['completed_at']))
            spans.append((pid, pid))
            prior = pid
        loss_rows, prior = [], after
        for loss in losses:
            require(isinstance(loss, dict) and integer(loss.get('from'), prior+1, cursor) and
                    integer(loss.get('to'), loss['from'], cursor) and loss.get('reason') == 'buffer_unavailable')
            loss_rows.append((loss['from'], loss['to'], loss['reason']))
            spans.append((loss['from'], loss['to']))
            prior = loss['to']
        expected = after+1
        for start, end in sorted(spans):
            require(start == expected)
            expected = end+1
        require(expected == cursor+1)
        envelope = compact({k: v for k, v in document.items() if k not in ('records', 'missing_ranges')})
        # The upstream envelope reserves 4 KiB. Allow additive metadata up to 16 KiB
        # while bounding its replication alongside every persisted publication.
        require(len(envelope.encode()) <= 16384)
        page = RawPage(node_id, expected_instance, stream, after, cursor, received_at_ms,
                       envelope, tuple(rows), tuple(loss_rows), previous_instance, previous_after)
        require(page.encoded_bytes <= 3 * 1024**2)
        return page
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
        raise protocol.ProtocolFailure('invalid replay page') from exc


@dataclass(frozen=True)
class SampledRecord:
    """Complete documents Hub already polled, stored under the replay identity (ADR-0007)."""
    node_id: str
    instance_id: str
    received_at_ms: int
    envelope_json: str
    rows: tuple  # (stream, publication id, payload JSON, reported start, reported end)

    @property
    def encoded_bytes(self):
        return len(self.envelope_json.encode()) + sum(len(r[2].encode()) + 256 for r in self.rows) + 1024


def sampled_envelope(path, poll_interval_ms):
    # gap_before is not part of a live document; sampled rows store 0 and declare it unobserved.
    return compact(dict(source='live_poll', path=path, capture='sampled',
                        gap_before_observed=False, poll_interval_ms=int(poll_interval_ms)))


def _row(stream, timing, payload):
    pid = timing.get('sequence' if stream == 'core' else 'id') if isinstance(timing, dict) else None
    if (not integer(pid, 1) or not finite(timing.get('started_at')) or not finite(timing.get('completed_at')) or
            len(payload.encode()) > 1024**2):
        return None
    return (stream, pid, payload, timing['started_at'], timing['completed_at'])


def sampled_core(node_id, stats, received_at_ms, poll_interval_ms):
    """A validated /api/stats document as one sampled core row, or None when it cannot be identified."""
    row = _row('core', stats.get('sample'), compact(stats))
    if row is None:
        return None
    return SampledRecord(node_id, stats['sample']['instance_id'], received_at_ms,
                         sampled_envelope('/api/stats', poll_interval_ms), (row,))


def sampled_observations(node_id, document, streams, received_at_ms, poll_interval_ms):
    """Group entries of a validated /api/observations document for the requested streams."""
    rows = []
    for name in streams:
        group = document['groups'].get(name)
        if isinstance(group, dict) and group.get('observation') is not None:
            row = _row(name, group['observation'], compact(group))
            if row is not None:
                rows.append(row)
    if not rows:
        return None
    return SampledRecord(node_id, document['instance_id'], received_at_ms,
                         sampled_envelope('/api/observations', poll_interval_ms), tuple(rows))
