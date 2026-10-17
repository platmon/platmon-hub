# ADR-0007: Per-node raw capture level, including sampled capture from live polls

[Documentation index](../../README.md) · [Design reference](../README.md)

- **Status:** Implemented in Hub; see [implementation notes](#implementation-notes-2026-10-17)
- **Date:** 2026-10-17
- **Scope:** Which raw publications Hub persists for each node, and from which source
- **Hub baseline:** `main` at `f4ba1b5e7bb5fe5795462e7ae4b11a5208747d46` (SQLite schema 7)
- **Related:** [ADR-0001](0001-http-pull-and-device-identity.md), [ADR-0003](0003-agent-replay-and-raw-publications.md), [ADR-0004](0004-downstream-data-handoff.md), [ADR-0006](0006-raw-payload-compression.md)

## Context

With `--raw-capture`, ADR-0003 persists every publication an Agent exposes through replay. Two
cases are not served:

- **Agents without replay.** Replay is unreleased in platmon; Agents on released 1.2.x answer 404,
  so Hub persists only the 5-second `core_samples` projection of 20 selected columns. ADR-0004
  hands off no measurements for such nodes.
- **Nodes that do not need 1 Hz data.** A long-running fleet device watched for slow degradation
  pays the full 1 Hz raw cost although a coarser series would do.

Hub already fetches complete documents for every node: `/api/stats` every 5 seconds and
`/api/observations` every 10 seconds, validated by `protocol.stats` and
`protocol.observations`, then discarded after projection. A core replay record's `data` has all
`/api/stats` fields plus `sample.stale`, identified by the same `sample.sequence` (ADR-0003
requires `sample.sequence == id`); a slow-group record is the complete group entry of
`/api/observations`, identified by its `observation.id`
([platmon replay reference](https://github.com/platmon/platmon/blob/main/docs/reference/replay.md)).

## Decision

Add a per-node **capture level**, and a **sampled** source that persists the documents Hub
already polls, in the existing `raw_publications` table under the existing identity.

| Level | Source | Resolution | Outage recovery | Needs replay |
| --- | --- | --- | --- | --- |
| `auto` (default with `--raw-capture`) | Replay where the stream supports it, otherwise sampled | 1 Hz core where replay exists, else poll cadence | Where replay exists | No |
| `replay` | Replay only, as ADR-0003 | Every publication | Within the Agent buffer | Yes |
| `sampled` | Live polls only | 5 s core; each slow group at most every 10 s | None | No |
| `off` | Nothing raw; live, projection and alerts unchanged | — | — | No |

Without `--raw-capture` every node is `off`, as today.

### Identity and coexistence

A sampled row uses the replay key `(node_id, instance_id, stream, publication_id)` with
`publication_id` = `sample.sequence` for core and `observation.id` for a slow group. Sampled and
replayed copies of a publication therefore collide and the existing `ON CONFLICT DO NOTHING` keeps
the first, as ADR-0003 already does for duplicate retries; their age fields may differ and do not
make a publication new. A slow group polled again before its next observation produces the same
key and is not stored twice.

In `auto`, a stream switches to sampled while replay for it is `unavailable` (HTTP 404) and back
to replay when replay succeeds again. Replay then fetches whatever the Agent buffer still holds,
and the overlap is deduplicated by key.

### Marking sampled rows

Sampled rows reference a content-addressed envelope in `raw_envelopes`, so no new column is
needed:

```json
{"source": "live_poll", "path": "/api/stats", "capture": "sampled",
 "gap_before_observed": false, "poll_interval_ms": 5000}
```

`gap_before` is not part of a live document. Sampled rows store `0`, and the envelope's
`gap_before_observed: false` tells readers that the column carries no evidence for those rows.
Sampled core rows store the polled document verbatim and therefore lack `sample.stale`, which only
replay adds; Hub does not synthesize it, and readers derive staleness from `data_age_ms` and
`stale_after_ms`. Intentional holes between sampled IDs are not losses: Hub records no `replay_losses` for them.
Hub-observed gaps and connectivity events are recorded as today. Exports and ADR-0004 segments
already carry the envelope with each row, so a consumer distinguishes sampled from replayed rows
without a format change.

### Ingestion path

The poll workers already hold the validated documents. After live acceptance, the scheduler
submits a bounded sampled-record command to the history writer, as it does for `core_samples`;
poll workers perform no database I/O and never wait for the writer. The command shares the raw
encoded-byte queue budget and is dropped, with a counter, when the queue is full. Sampled
ingestion touches no replay cursor and never affects live health or alerts. Storage uses
[ADR-0006](0006-raw-payload-compression.md) compression when available.

### Configuration

Add an optional `capture` field to registry records, one of `auto`, `replay`, `sampled` or `off`,
default `auto`. The nodes CLI, the opt-in web device management and registry reload validate it
like the other fields. A registry that uses `capture` is rejected by older Hub versions, which
accept only `node_id`, `name`, `endpoint` and `enabled`; downgrades therefore need the field removed.
`--raw-capture` remains the global switch and still requires raw retention and quota.

## Capacity

Sampled core capture stores one publication in five on the host measured for ADR-0004:

| Per node-day | 1 Hz replay | 5 s sampled |
| --- | --- | --- |
| Compact JSON (ADR-0003) | about 1.7 GiB | about 0.34 GiB |
| With ADR-0006 compression | about 0.2 GiB | about 0.04 GiB |

Slow groups add little: they are under 2 % of raw payload at their own cadence.

## Relationship to ADR-0004

ADR-0004 hands off `raw_publications` regardless of source, so sampled nodes now deliver full
measurements. Its statement that upgrading the Agent is the only way to obtain measurements from a
node without replay is superseded: a node without replay delivers sampled rows unless its level is
`off`, and `nodes.jsonl` reports each node's configured level and each stream's effective source.
ADR-0004's rejection of handing off `core_samples` and `observations` projections still stands;
sampled rows are complete documents, not projections.

## Alternatives

| Alternative | Decision |
| --- | --- |
| Hand off projections (`core_samples`) for nodes without replay | Reject, as in ADR-0004: 20 selected columns do not serve the analyses |
| Poll `/api/stats` faster for sampled nodes | Reject: changes live load and the ADR-0001 polling contract to obtain storage |
| Decimate replay (keep every Nth publication) | Reject: downloads all data to discard most of it; sampled already has the documents |
| A separate table for sampled rows | Reject: duplicates identity, retention, quota, export and handoff logic |
| Global level only | Reject: fleets mix Agents with and without replay and devices with different needs |

## Consequences

Every node can contribute full measurements, including Agents on released 1.2.x, and operators
can trade resolution for storage per node. Sampled data has no outage recovery and coarser
resolution; consumers read the envelope to tell sampled from replayed rows. The registry format
gains one optional field.

## Implementation and acceptance

1. Registry field, CLI, web device management, reload and documentation.
2. Sampled-record writer command, envelope, queue accounting and counters.
3. `auto` switching on replay availability per stream; status reports configured level and
   effective source per stream.
4. ADR-0004 `nodes.jsonl` fields, when that ADR is implemented.

Acceptance covers: each level against Agents with and without replay; switching from sampled to
replay and back with overlapping IDs; slow groups polled more often than they publish; no replay
losses for intentional holes; queue-full drops without live impact; registry validation, reload
and rejection by an older registry parser; export of mixed sampled and replayed rows; and every
interpreter supported under [ADR-0005](0005-os-and-python-support.md).

## Implementation notes (2026-10-17)

The registry accepts `capture`; `platmon-hub nodes set-capture` sets it, other CLI and web
edits keep it, and the default `auto` is not written, so registries that never set a level
stay readable by older Hubs. A capture-only change is recorded as a `registry_changed`
event with reason `capture_changed`. `auto` switches a stream to sampled once its replay
answered 404 and back after the next successful replay commit. `/api/replay/status` adds a
`capture` map and a per-stream `source`; `history.raw` adds `sampled_rows`,
`sampled_duplicates` and `sampled_failures`. `nodes.jsonl` fields wait for ADR-0004.

Checked on 2026-10-17 against the installed platmon 1.3.0 service with `capture: sampled`
for 32 seconds: no replay request, core rows every five seconds, slow groups deduplicated
(four repeated observations), every envelope `live_poll`, stored compressed at 5.3 % of the
JSON size.
