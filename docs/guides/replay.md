# Full-publication replay and raw storage

[User guide](README.md) · [Configuration](configuration.md) · [ADR-0003](../design/adr/0003-agent-replay-and-raw-publications.md)

Raw capture recovers complete core and storage/Wi-Fi/battery/probe publications
from replay-capable Agents, including fields absent from selected History rows.
It is disabled by default. Live monitoring, alerts, existing History projections,
CSV archives and reports keep their existing semantics. Historical projections
and AI integration are later consumers of the committed raw data.

## Enable capture

Choose retention and capacity from measurements of your intended fleet and storage.
Both raw retention and raw quota must be supplied explicitly with `--history-db`:

```sh
python3 -m hub --nodes /private/nodes.json --history-db /private/history.db --raw-capture --raw-retention-days 0.25 --raw-quota-mib 1024
```

On Windows use `python` instead of `python3` and local Windows paths. The values
above are a six-hour/1 GiB syntax example, not recommended fleet capacity. A fleet
of 100 nodes publishing 10 KiB at 1 Hz produces about 82.4 GiB of payload per day,
before indexes, envelopes and WAL. Measure actual payloads first. Raw retention
does not inherit the seven-day summary retention default.

An ordered migration to SQLite schema 6 preserves a verified schema-5 backup. Schema 8
stores new raw payloads compressed ([ADR-0006](../design/adr/0006-raw-payload-compression.md)):
the writer verifies each row decodes to the identical JSON before committing it and
otherwise stores the JSON. Exports decompress, so their content is unchanged.
Protect raw data, migration backups and exports: complete publications can contain
hostnames, sensor labels and device/probe metadata. The database is not encrypted.
Restoring a backup is required for a code downgrade; never lower `user_version`.

## Capture levels

Each registry node may set `capture` ([ADR-0007](../design/adr/0007-sampled-capture-level.md));
omitted means `auto`. Set it with `platmon-hub nodes --file ... set-capture NODE_UUID LEVEL`.

| Level | Raw rows come from |
| --- | --- |
| `auto` | Replay where the Agent stream supports it; otherwise the documents Hub already polls |
| `replay` | Replay only |
| `sampled` | Live polls only: core every 5 s, each slow group at most every 10 s; no replay requests |
| `off` | Nothing raw for this node; Live, history and alerts are unchanged |

Sampled rows use the replay identity, so a publication captured both ways is stored once
(the first copy). Their envelope says `source: live_poll` and `gap_before_observed: false`:
`gap_before` is stored as 0 but is not evidence, and sampled core rows lack the
`sample.stale` field that only replay adds. Holes between sampled IDs are not losses and
produce no `replay_losses`. Sampled capture has no outage recovery. It shares the raw queue;
a full queue drops the record and counts it in `history.raw.rejected`. Agents on platmon
1.2.x and older answer replay with 404, so under `auto` they deliver sampled rows.

## Coverage and failure behavior

Read `GET /api/replay/status` for per-node/per-stream state, last durable cursor,
instance, backlog, committed-page count and last error. `GET /api/history/status`
adds `history.raw` queue bytes/capacity, committed counts, storage errors, quota
and retention settings, and compression counters: `compressed_rows`, `json_rows`,
`compression_fallback`, live `dictionaries` and `dictionary_bytes` (as of the last
committed page), `logical_bytes`, `stored_bytes` and `stored_logical_ratio` since start, and
sampled capture's `sampled_rows`, `sampled_duplicates` and `sampled_failures`. These endpoints do not expose payloads or Agent URLs.

- `captured`: the last page committed; it is not a promise of complete history.
- `committing`: queue admission succeeded, but the cursor is not yet durable.
- `unavailable`: Agent/group returned 404; Live continues; capability is retried
  after 60 seconds. Availability is checked per Agent, independently of version.
- `retrying`: malformed page, transport, identity race, storage failure or full
  queue; retry uses a fresh durable cursor with bounded backoff (2–60 seconds).
- `stopped`: unsupported required schema or `cursor_ahead`; fix compatibility or
  configuration and restart Hub, or change the registry generation. No cursor reset
  happens silently. Rename alone preserves the generation and does not reset it.

Each turn confirms process identity with status, then reads one bounded replay page.
A restart race (409 `instance_changed`) is retried after another status confirmation;
the next successful new-generation commit records an unknown final extent for the
old process. Each node has at most one Live request chain and one replay turn in
flight, sharing at most 16 workers. Due Live work receives slots first, and a replay
turn does not hold the node's Live slot. Each stream gets round-robin page turns.
Replay has 2 MiB responses and up to 100 records; Live retains its 512 KiB limit.
The replay envelope is additionally limited to 16 KiB of metadata. Redirects and
arbitrary paths/parameters are rejected. Replay never updates health or alerts.

Core `gap_before` denotes failed/too-slow attempts without publication IDs.
Unavailable inclusive ID ranges denote lost published records. Agent restart
clears its buffer; expiration/capacity can shorten its nominal ten-minute window.
No interpolation, zero filling or exact outage timestamp is invented.

## Storage limits

Before a page GET, the collector reserves 3 MiB of encoded capacity and one queue
slot, releasing the reservation on completion or registry invalidation. This
backpressure limits concurrent catch-up before expensive responses are decoded.

The independent ingress queue defaults to 8 MiB of encoded pages/bookkeeping
(`--raw-queue-mib`, 3–64 MiB), with at most 16 pending commands/pages. Decoded HTTP
responses, Python objects and allocator overhead add memory outside that budget.
A page is acknowledged after its records, loss evidence and cursor commit together.
Duplicates preserve the first payload and receipt time. On failure, none advances.

The raw quota charges retained stored payload bytes (compressed, or JSON for legacy and
fallback rows) plus a 256-byte per-row reservation, each page envelope once (plus 256
bytes) however many rows share it, and each compression dictionary once (plus 256 bytes). At quota, a committing page first evicts the oldest committed
raw rows (by Hub receipt time) to make room; retention is an upper bound, not a
guarantee under quota pressure. Only a page too large to fit even after eviction rolls
back, and capture backs off with committed cursors unchanged. If the quota is lowered,
maintenance evicts excess rows in chunks of up to 1000. Expired rows are deleted in chunks
of up to 1000, normally every minute, with shorter follow-up turns while draining.
Cursor and loss metadata are retained independently so expired rows are not
repeatedly fetched and earlier loss evidence stays available.

The raw quota is not a hard SQLite file-size cap. Indexes, WAL, cursor/loss metadata,
backups, exports and archives need separate filesystem capacity. There is no
automatic VACUUM. A large wall-clock discontinuity suspends time-based pruning;
correct the clock and restart Hub. Quota eviction does not use wall time and continues. Retention uses first Hub receipt time, so a
recovered backlog is retained from its commit receipt, not its measurement age.

## Export committed raw data

```sh
python3 -m hub raw-export --db /private/history.db --output /private/raw.zip --from-ms 1700000000000 --to-ms 1700000060000
```

Specify your actual half-open Hub receipt UTC millisecond range (at most 31 days).
Optional repeated `--node-id UUID` filters nodes. The new ZIP is never overwritten;
a failed export publishes no partial file. A consistent read-only transaction
streams `raw_publications.jsonl`, `replay_losses.jsonl`, `replay_cursors.jsonl`
and `manifest.json`. Cursor snapshots are independent of the receipt-time filter.

Defaults are a 30-second deadline, 100000 rows across all three JSONL files and
512 MiB uncompressed output. `--timeout` allows up to 120 seconds, `--max-rows` up
to 1000000, and `--max-bytes` up to 512 MiB. Bounds fail the export visibly.
Original Agent collection wall times (Unix seconds), elapsed ages/durations
(milliseconds), source metadata, units, nulls and valid zeros remain distinct from
Hub receipt UTC milliseconds. Receipt time is never substituted for collection time.
