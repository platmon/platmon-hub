# Hub read-only API

[Documentation index](../README.md) · [Design reference](README.md)

Hub HTTP schema 1 is independent of Agent schemas and SQLite schema 9. JSON missing
measurements are `null`, never synthetic zero. UTC timestamps use Unix milliseconds.
Responses disable caching. The browser contacts the Hub only.

| GET path | Purpose |
| --- | --- |
| `/healthz` | Process responsiveness (`status: ok`), not fleet/storage health |
| `/api/nodes` | Current fleet summaries, `schema_version` and `nodes` |
| `/api/nodes/{node_id}` | Detail, last-known readings and freshness |
| `/api/nodes/{node_id}/history` | Bounded core metric series |
| `/api/history/status` | Writer, queue, drops, retention and storage diagnostics, including `history.raw` storage/byte-budget counters |
| `/api/replay/status` | Replay coverage by stable node ID and stream; disabled unless raw capture is enabled |
| `/api/registry/status` | Reload diagnostics |
| `/api/events` | Persisted transitions with filters and pagination |
| `/api/alerts` | Current internal alerts and restoration/admission diagnostics |

Node routes use the canonical registry UUID. Unknown nodes return 404. A removed
node's stored data remains in SQLite, but the node History route requires a current
registry entry. See [backend](../guides/setup.md) for live payload and freshness semantics.

`overview.npu_percent` is an additive nullable percentage beside `gpu_percent`.
Detail `stats.gpu` / `stats.npu` preserve driver/provider and clocks (Hz), and
`observations.battery` preserves the independent battery envelope and its age.
Clients must distinguish unknown/null readings from numeric zero, and use the
group's freshness rather than core freshness for battery data.

Node summaries also carry additive nullable alert evidence from current observations:
`probe_failure_percent`, the highest `failure_ratio` × 100 among the probe targets;
`battery_percent`, the lowest charge among present batteries; and `wifi_signal_dbm`,
the weakest signal among connected Wi-Fi interfaces. Each is null when the group is
absent, starting, stale or unsupported, or has no qualifying row.

A `pressure` object carries `psi_cpu_some_avg10`, `psi_memory_some_avg10` and
`psi_io_some_avg10` from the last accepted core sample, for alerts; each is null when
the Agent reports no such resource. Like `overview`, consult `data_current` first.

## History

Required query parameters are `metric`, `from_ms`, `to_ms`; optional `max_points`
defaults to 1200 and must be 1–1200. The half-open interval `[from_ms, to_ms)` must
be positive and at most 7 days. Metrics are a fixed allowlist: `cpu_percent`,
`gpu_percent`, `npu_percent`, `memory_percent`, `disk_percent`, `hottest_c`, and the
schema-7 history-only `psi_cpu_some_avg10`, `psi_memory_some_avg10`, `psi_io_some_avg10`, `network_rx_busiest_bytes_per_s`, `network_tx_busiest_bytes_per_s`, `disk_read_bytes_per_s`, `disk_write_bytes_per_s` (see [history schema](history-schema.md)). Node summaries do not include them.

The response includes `schema_version`, `node_id`, `metric`, `from_ms`, `to_ms`,
`max_points`, `points`, `gaps`, `timestamp_meaning: hub_receipt_utc_ms` and
`missing: null` (the latter is a descriptive string). Each point has `timestamp_ms`
(the first receipt in its bucket), `instance_id`, `segment`, `average`, `minimum`,
`maximum` and `count` of non-null readings. Null-only buckets have null aggregates
and count 0. Buckets never bridge instance changes, null boundaries, gap evidence
or receipt silence longer than 15 seconds. Gaps contain start/end UTC milliseconds,
reason and instance; an open gap has a null end. Unknown silence is not proof of a
network outage. No unobserved samples are generated.

Invalid or repeated/unknown parameters return 400; disabled/unavailable history or
a two-second query deadline returns 503. Too many discontinuities to represent
within the point budget return 422; narrow the interval. Empty history returns 200
with empty arrays. The API does not expose full Agent payloads or optional series.

## Events and alerts

Events accept `from_ms`, `to_ms`, `node_id`, `type`, `limit`, `cursor`. Default range
is the last 24 hours; maximum is 30 days. Limit defaults to 100, maximum 500. Type
uses the implementation's fixed event allowlist. Responses contain `events`,
`next_cursor`, range and schema. Events include `event_id`, nullable `node_id`,
`event_type`, `received_at_ms`, `previous_state`, `current_state`, `reason_code`.
Order is descending receipt time and event UUID. Reuse the returned range and
opaque `next_cursor` for subsequent pages. Invalid parameters return 400;
unavailable persistence returns 503.

Alerts expose at most 500 non-Normal records plus diagnostics, with no mutation or
acknowledgement endpoint. Queue admission is not proof of a committed alert event.
Rules and restoration behavior are defined in [alerts](../guides/alerts.md).

## Access and limits

POST, PUT, PATCH and DELETE return 405 by default. The opt-in local-only
registry POST exceptions and local listing are documented in
[web registration](../guides/web-registration.md). Static assets use exact paths without query
strings. Requests must use an accepted loopback Host header. Limits include 32
connections, five-second socket inactivity, 4 KiB request targets, 16 KiB parsed
headers and a shared GET budget of 100/second with burst 200. Excess load can return
429 or 503; clients should back off. These controls do not replace TLS or user
authentication. See [security](../guides/security.md) for the optional proxy and OS-local
administration boundary.

## Data downloads

`GET /api/data/export?from_ms=...&to_ms=...&node_id=...` downloads a bounded
CSV ZIP of persisted rows (not graph buckets). `node_id` is optional and may refer
to a removed registration. See [data-export.md](../guides/data-export.md) for format 1,
limits and errors. All other methods remain 405. Existing security headers and
request admission apply. Archives are private operational material.

`GET /api/data/report` accepts the same selection and limits as the export route
and returns daily/monthly summaries of observed samples and recorded transitions.
`GET /api/data/archive/status` exposes opt-in archival state, failures and the
last successful generation time, without private directory paths. Neither route
permits writes or accepts an Agent/network URL.

## Replay diagnostics

`GET /api/replay/status` returns `{schema_version: 1, replay: {enabled, nodes, capture}}`.
`capture` maps each node UUID to its configured raw capture level (`auto`, `replay`,
`sampled` or `off`; [ADR-0007](adr/0007-sampled-capture-level.md)), and every stream also
reports its effective `source`: `replay`, `sampled` or `off`.
When disabled only `enabled: false` is returned. `nodes` is keyed by Hub node UUID,
then by `core`, `storage`, `wifi`, `battery`, `probe`. Each stream reports `state`,
`error`, last durable `instance_id` and acknowledged `after_id`,
`has_more`, acknowledged `pages`, observed missing-range `losses` count and `source`. States
include `starting`, `fetching`, `committing`, `captured`, `retrying`, `unavailable`
and `stopped`. In-memory counters reset on Hub restart. The instance/cursor pair changes only after a durable cursor read or acknowledged
commit; it remains unchanged while a new page is committing.

`GET /api/history/status` additionally reports `history.raw`: `enabled`, encoded
`queued_bytes`, `queue_byte_limit`, command/page `queue_depth`, `queue_capacity`,
`in_flight`, explicit `quota_bytes`/`retention_ms`, `committed_pages`, `persisted`,
`duplicates`, `rejected`, `failed_pages`, `cursor_read_failures`, `retention_deleted`,
`quota_evicted`, `unflushed_pages` (pages that failed during shutdown) and `last_error`. Admission is not a
commit. Replay errors do not change the existing node health/alert fields.

Full raw payloads are available through the bounded local `raw-export` CLI, not a
browser-supplied Agent URL. See [raw capture/export](../guides/replay.md).
