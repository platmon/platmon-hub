# History schema and durability

[Documentation index](../README.md) · [Design reference](README.md)

SQLite `PRAGMA user_version` is **7**, independent of application and HTTP versions.
The executable schema is [hub/history/schema.py](../../hub/history/schema.py); the
[ADR](adr/0002-hub-history-storage.md) records initial design decisions.

| Version | Added storage |
| --- | --- |
| 1 | `core_samples`, node/time and retention indexes |
| 2 | `observations`, `gaps`, `continuity`, `hub_sessions` |
| 3 | `events`, event indexes, continuity headline |
| 4 | `alert_states` with rule configuration and restoration state |
| 5 | Nullable `core_samples.npu_percent`; existing rows retain NULL |
| 6 | Opt-in `raw_publications`, `replay_cursors`, `replay_losses`, indexed encoded-byte accounting in `raw_usage` |
| 7 | Nullable pressure and I/O rate columns on `core_samples`; existing rows retain NULL |
| 8 | `raw_dictionaries` and `raw_publications.payload_zlib`/`dictionary_id`: new raw payloads are `zlib` with a shared, reference-counted dictionary ([ADR-0006](adr/0006-raw-payload-compression.md)); existing rows stay JSON |
| 9 | Handoff bookkeeping ([ADR-0004](adr/0004-downstream-data-handoff.md)): `handoff_state` (store identity, counter, delivered/acknowledged sequence), `handoff_losses`, nullable `handoff_seq` on raw publications, replay losses, gaps, events and Hub sessions assigned by triggers only while handoff is enabled, and `gaps.gap_uid` |

Core identity is `(node_id, instance_id, sequence)`. Columns store Hub receipt UTC
milliseconds, reported `data_age_ms`, `stale_after_ms`, accepted core state
(`ready`/`degraded`), CPU/GPU/NPU/memory/disk percentages and highest Celsius temperature.

Schema 7 adds load columns taken from the same accepted `/api/stats` sample:

| Column | Source and meaning |
| --- | --- |
| `psi_cpu_some_avg10`, `psi_memory_some_avg10`, `psi_io_some_avg10` | `pressure.resources.<name>.some.avg10`, percent 0–100 |
| `network_rx_busiest_bytes_per_s`, `network_tx_busiest_bytes_per_s` | Highest `rates.<dir>_bytes_per_s` of any interface except `lo`, per direction independently |
| `disk_read_bytes_per_s`, `disk_write_bytes_per_s` | Sum of `rates.<dir>_bytes_per_s` over the reported physical whole disks |

Network interfaces are never summed: one packet can cross a bridge and its port, so a
total would not be the host's traffic; the receive and send maxima may come from different
interfaces. Disk rates are summed because the Agent reports non-overlapping whole disks;
if any listed disk has no rate (for example while its rate warms up), the total is NULL
rather than an undercount. A disabled or unavailable Agent group, or PSI not built into the
kernel, leaves its columns NULL. Network rates describe the Agent's network namespace.
Optional identity is `(node_id, instance_id, group_name, observation_id)`; it never
uses core sequence. Only selected storage/Wi-Fi/probe/battery fields are projected into a
bounded JSON field (16 KiB per group). These summary tables do not persist responses wholesale; opt-in raw capture uses separate tables below.
Missing readings stay SQL NULL / JSON null; a valid zero is retained.

Battery records retain separate charge/energy families in µAh/µWh, voltage in µV,
signed current/power in µA/µW, cycle count, driver state text and tri-state
presence/external-power flags. They share the observation table's existing
identity, byte bound and retention. No battery time series is synthesized from
these rows; raw observation exports carry the selected data.

Samples must be newly accepted, schema-valid and fresh. Duplicate and backward
sequence samples in the same instance are not new records. Instance changes create
separate generations. Receipt time is not an invented Agent wall-clock timestamp.
Monotonic clocks govern runtime freshness, scheduling and alert durations; they
are not persisted as globally meaningful time. A large wall-clock jump suspends
retention until the process is restarted after clock correction.

Gap records and continuity/session metadata preserve observed discontinuities,
including stale, transport, restart, optional and storage-loss evidence. Bounds
are approximate; Hub downtime and unexplained silence remain unobserved/unknown.
Storage cannot reliably record its own every failure while unavailable. Counters
and in-memory diagnostics complement the retained evidence; no zero-filled or
interpolated rows are inserted.

## Writer and retention

One process owns the database writer lock. One writer thread uses WAL, FULL
synchronous mode, a 250 ms busy timeout and batches of up to 128 records. Polling
only submits to a nonblocking bounded queue outside NodeState locks. Readers use
separate read-only connections. Failed writes and overflow are counted, not
reported as successful persistence. Shutdown attempts a bounded drain; abrupt
termination can lose admitted but uncommitted records.

Core and optional retention defaults to 7 days, events to 30 days, with hourly
bounded transactional cleanup. Alert snapshots are current restoration state,
not a full time series. Deletion does not shrink the file immediately; do not
run uncontrolled VACUUM against an operating Hub. WAL/checkpoint and disk budgets
must be measured for the deployment; see [resource budget](../development/resource-budget.md).

## Migration and recovery

The writer checks integrity and applies ordered transactional migrations, creating
a verified pre-migration backup before each upgrade of an existing database. A database
created on first open has nothing to preserve and gets no backups. An unsupported future schema
or migration failure disables history while live monitoring continues. Do not
lower `user_version` manually. There is no automatic schema downgrade.

Use `platmon-hub backup --db PATH --output NEW_PATH` for a consistent online copy;
it refuses overwrite and verifies the backup. Never copy only the live main DB
while ignoring WAL. Restore to a clean path after all users of that database have
stopped. Preserve the old DB and sidecars for diagnosis. The [release policy](../development/release.md)
and [operations guide](../guides/operations.md) describe version selection and data-loss
consent for restoring an older backup.

## Raw publications and replay cursors (schema 6)

The separate opt-in raw path stores complete validated payload JSON, the replay
envelope (schema, clock and capture bounds), original reported collection wall times,
Hub receipt milliseconds, `gap_before`, stream and publication ID. Identity is
`(node_id, instance_id, stream, publication_id)`. Stale and older-than-Live samples
are valid here; they never enter the live summary acceptance path.
`raw_envelopes` stores each page envelope once, keyed by SHA-256 digest; publications
reference it by `envelope_id`, and triggers keep a reference count so the envelope is
deleted with its last publication. `raw-export` joins it back into each row.

`replay_cursors` retains each generation's committed `after_id`; a unique partial
index selects one active generation per node/stream. `replay_losses` stores
inclusive unavailable ranges and restart boundaries with unknown final IDs.
A page's rows, loss evidence, generation switch and monotonic cursor update are
one transaction. Targeted uniqueness conflicts preserve first-received payloads.
The writer returns a completion future after commit, never on queue admission.
Cursor loads also execute on the writer thread; poll workers do no SQLite I/O.

`raw_usage` and insert/delete triggers maintain transactional encoded-byte quota
accounting (each envelope charged once) without a fleet-size SUM on every page. At quota, a page commit evicts
the oldest previously committed raw rows (counted as `quota_evicted`); only a page
that cannot fit on its own rolls back. Separate finite retention prunes raw rows in bounded turns; cursor/loss
metadata survives. Metadata, indexes, WAL, backups and exports remain separate
filesystem costs. See [raw capture](../guides/replay.md) for configuration and export.
