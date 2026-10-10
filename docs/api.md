# Hub read-only API

Hub HTTP schema 1 is independent of Agent schemas and SQLite schema 4. JSON missing
measurements are `null`, never synthetic zero. UTC timestamps use Unix milliseconds.
Responses disable caching. The browser contacts the Hub only.

| GET path | Purpose |
| --- | --- |
| `/healthz` | Process responsiveness (`status: ok`), not fleet/storage health |
| `/api/nodes` | Current fleet summaries, `schema_version` and `nodes` |
| `/api/nodes/{node_id}` | Detail, last-known readings and freshness |
| `/api/nodes/{node_id}/history` | Bounded core metric series |
| `/api/history/status` | Writer, queue, drops, retention and storage diagnostics |
| `/api/registry/status` | Reload diagnostics |
| `/api/events` | Persisted transitions with filters and pagination |
| `/api/alerts` | Current internal alerts and restoration/admission diagnostics |

Node routes use the canonical registry UUID. Unknown nodes return 404. A removed
node's stored data remains in SQLite, but the node History route requires a current
registry entry. See [backend](../backend.md) for live payload and freshness semantics.

## History

Required query parameters are `metric`, `from_ms`, `to_ms`; optional `max_points`
defaults to 1200 and must be 1–1200. The half-open interval `[from_ms, to_ms)` must
be positive and at most 7 days. Metrics are a fixed allowlist: `cpu_percent`,
`gpu_percent`, `memory_percent`, `disk_percent`, `hottest_c`.

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
Rules and restoration behavior are defined in [alerts](alerts.md).

## Access and limits

POST, PUT, PATCH and DELETE return 405 by default. The opt-in local-only
registry POST exceptions and local listing are documented in
[web registration](web-registration.md). Static assets use exact paths without query
strings. Requests must use an accepted loopback Host header. Limits include 32
connections, five-second socket inactivity, 4 KiB request targets, 16 KiB parsed
headers and a shared GET budget of 100/second with burst 200. Excess load can return
429 or 503; clients should back off. These controls do not replace TLS or user
authentication. See [security](security.md) for the optional proxy and OS-local
administration boundary.

## Data downloads

`GET /api/data/export?from_ms=...&to_ms=...&node_id=...` downloads a bounded
CSV ZIP of persisted rows (not graph buckets). `node_id` is optional and may refer
to a removed registration. See [data-export.md](data-export.md) for format 1,
limits and errors. All other methods remain 405. Existing security headers and
request admission apply. Archives are private operational material.

`GET /api/data/report` accepts the same selection and limits as the export route
and returns daily/monthly summaries of observed samples and recorded transitions.
`GET /api/data/archive/status` exposes opt-in archival state, failures and the
last successful generation time, without private directory paths. Neither route
permits writes or accepts an Agent/network URL.
