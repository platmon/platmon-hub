# History schema and durability

SQLite `PRAGMA user_version` is **4**, independent of application and HTTP versions.
The executable schema is [hub/history/schema.py](../hub/history/schema.py); the
[ADR](adr/0002-hub-history-storage.md) records initial design decisions.

| Version | Added storage |
| --- | --- |
| 1 | `core_samples`, node/time and retention indexes |
| 2 | `observations`, `gaps`, `continuity`, `hub_sessions` |
| 3 | `events`, event indexes, continuity headline |
| 4 | `alert_states` with rule configuration and restoration state |

Core identity is `(node_id, instance_id, sequence)`. Columns store Hub receipt UTC
milliseconds, reported `data_age_ms`, `stale_after_ms`, accepted core state
(`ready`/`degraded`), CPU/GPU/memory/disk percentages and highest Celsius temperature.
Optional identity is `(node_id, instance_id, group_name, observation_id)`; it never
uses core sequence. Only selected storage/Wi-Fi/probe fields are projected into a
bounded JSON field (16 KiB per group). Raw responses are not persisted wholesale.
Missing readings stay SQL NULL / JSON null; a valid zero is retained.

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
must be measured for the deployment; see [resource budget](resource-budget.md).

## Migration and recovery

The writer checks integrity and applies ordered transactional migrations, creating
a verified pre-migration backup before each upgrade. An unsupported future schema
or migration failure disables history while live monitoring continues. Do not
lower `user_version` manually. There is no automatic schema downgrade.

Use `platmon-hub backup --db PATH --output NEW_PATH` for a consistent online copy;
it refuses overwrite and verifies the backup. Never copy only the live main DB
while ignoring WAL. Restore to a clean path after all users of that database have
stopped. Preserve the old DB and sidecars for diagnosis. The [release policy](release.md)
and [operations guide](operations.md) describe version selection and data-loss
consent for restoring an older backup.
