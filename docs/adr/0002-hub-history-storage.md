# ADR-0002: Hub-owned history with an isolated SQLite writer

[Documentation index](../README.md)

- **Status:** Proposed for Draft PR review; implementation follows in PR #7
- **Date:** 2026-10-10
- **Baseline:** `main` at `bdb77b5398751ad34c91711bbbe02191a5839f87`
- **Scope:** Core history storage contract; extension boundaries for observations, gaps, queries and events
- **Related:** [ADR-0001](0001-http-pull-and-device-identity.md), [Architecture](../architecture.md), [Agent contract](../agent-contract.md), [Backend](../backend.md)

## Context and decision

The current `Fleet` polls fixed agent GET endpoints and `NodeState` retains only
the latest accepted values. Restarting Hub loses that cache. Agent history is
volatile and cannot establish what Hub actually observed. Preserve the current
polling, freshness and optional-error semantics while adding opt-in persistence.

Use Python 3.9+ standard-library `sqlite3`, a bounded memory queue and one writer
thread. Enable history only with an explicit `--history-db PATH`; otherwise do
not create a database, writer or queue. No runtime dependency, agent change,
new polling endpoint, registry write API or browser change belongs to this ADR.
The database is owned by Hub; its schema version is independent of Hub HTTP
schema 1 and agent status/stats schema 1 and observations schema 2.

History records successful observations, not every sample the agent produced.
Never poll `/api/history`, backfill from cached values, manufacture samples during
outages or interpret missing metrics as zero. Storage failure must not change
node health or stop polling, the live API or independently running agents.

## Acceptance, identity and projection

Core identity is `(node_id, instance_id, sequence)`. `node_id` is the existing
canonical Hub UUID. Treat `instance_id` as an opaque agent identifier, not the
device identity. Equal sequences from different instances are distinct samples.
Within the currently tracked instance, reject a lower sequence. An equal
sequence may refresh live-cache age under existing behavior but creates no new
history record. A new instance starts a new generation; never merge by endpoint,
hostname or wall-clock time.

`NodeState.accept_core()` currently returns no acceptance result. PR #7 will
return an explicit result with `new`, `duplicate`, or `rejected` disposition and
an optional immutable core record. Validate status/stats instance agreement and
freshness first. Only `new` produces a record. Capture values and identity under
the state lock; release it before queue submission. Do not enqueue from a later
`snapshot()`, which could describe a different sample or cached age. A failed
projection or enqueue must not undo live acceptance.

Capture Hub receipt time immediately after the stats HTTP response is decoded,
before acceptance and queueing; pass it into acceptance. Do not use writer commit
time or the later status response timestamp. Store first-observed metadata and
values unchanged when the same key is received again. A database unique key is
the final duplicate guard after Hub restart; do not overwrite on conflict.
After restart, the writer initializes its per-instance sequence high-water mark
from retained history before inserting records for that instance, independently
of live cache initialization. It counts equal keys as duplicates and lower
sequences as rejected regressions; poll workers never query SQLite for this check.
Advance the durable mark only after commit. Once that instance's retained rows are gone, cross-restart
deduplication is no longer guaranteed; indefinite identity tombstones are outside
this bounded-retention contract.

The metric projection must share the existing `_overview()` rules rather than
create a second interpretation: CPU is the mean of valid per-core usage values;
GPU is valid reported usage; memory and disk are validated used/total percentages;
disk describes the existing root-filesystem metric; temperature is the maximum
finite temperature. Preserve existing rounding. Missing/unsupported/invalid
metrics are SQL NULL and JSON `null`, including a sample with all metrics absent.
Do not persist the agent response, sensor labels, endpoint or arbitrary JSON.

## Version 1 schema

The following is executable schema design, not an installed migration in this PR.
Application validation additionally requires canonical UUIDs, finite numbers and
integer fields of the right Python type (booleans are not numbers). Sequences
must fit SQLite's signed 64-bit integer. A history-only size/range rejection
increments diagnostics without tightening the existing live protocol contract.
An instance identifier is limited to 128 UTF-8 bytes for storage; never truncate
identifiers or convert them into a collision-prone substitute.

```sql
CREATE TABLE core_samples (
    node_id TEXT NOT NULL CHECK (length(node_id) = 36),
    instance_id TEXT NOT NULL
        CHECK (length(CAST(instance_id AS BLOB)) BETWEEN 1 AND 128),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
    data_age_ms REAL NOT NULL CHECK (data_age_ms >= 0),
    stale_after_ms REAL NOT NULL CHECK (stale_after_ms > 0),
    core_state TEXT NOT NULL CHECK (core_state IN ('ready', 'degraded')),
    cpu_percent REAL CHECK (cpu_percent BETWEEN 0 AND 100),
    gpu_percent REAL CHECK (gpu_percent BETWEEN 0 AND 100),
    memory_percent REAL CHECK (memory_percent BETWEEN 0 AND 100),
    disk_percent REAL CHECK (disk_percent BETWEEN 0 AND 100),
    hottest_c REAL,
    PRIMARY KEY (node_id, instance_id, sequence),
    CHECK (data_age_ms <= stale_after_ms)
);
CREATE INDEX core_samples_node_time
    ON core_samples (node_id, received_at_ms, instance_id, sequence);
CREATE INDEX core_samples_retention ON core_samples (received_at_ms);
PRAGMA user_version = 1;
```

`core_state` is the agent status state at acceptance, not the collector's `ok`
value or a later Hub headline. Only fresh ready/degraded samples reach this table.
Offline/error/stale transitions belong to separate gap/event records, never to
synthetic metric rows. There is no foreign key to the current registry: removing
or disabling a device must not cascade-delete its history.

Extension columns/tables require a named purpose, units, null semantics, query
contract and migration. Version 1 contains no extension blob. Future structured
optional payloads have an initial ceiling of 16 KiB UTF-8 per group record,
including labels; exceeding it is a reported storage rejection, not truncation.
That ceiling is a design limit, not permission to store whole agent responses.

## SQLite connections and transactions

Choose WAL for local disk storage with concurrent short readers. WAL requires
same-host access and introduces `-wal`/`-shm` files; network shares are unsupported.
Verify that `PRAGMA journal_mode=WAL` actually returns `wal`. Use
`synchronous=FULL` for the initial durability policy. Checkpointing and prolonged
read transactions can affect write cost and WAL growth. These tradeoffs follow
the [SQLite WAL documentation](https://www.sqlite.org/wal.html).

The writer owns and closes its connection on its own thread, retaining sqlite3's
thread-affinity checks. All mutations, migration and retention run there. Use
`isolation_level=None` and explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`,
compatible with Python 3.9; do not depend on newer `autocommit` arguments.
Set `busy_timeout=250` milliseconds on each connection. Lock waits and retries
must remain entirely off poll workers and outside `NodeState` locks.

Batch up to 128 records, flushing at least every 1 second while healthy. Each
batch is one transaction. Use a targeted primary-key conflict no-op; other
constraint errors must be reported, not hidden by blanket `INSERT OR IGNORE`.
Count persisted rows only after commit and count duplicate conflicts separately.
On rollback, no row in the batch counts as persisted. An ambiguous commit result
may be retried because inserts are idempotent.

Future query handlers use separate, short-lived, thread-local read connections
with `PRAGMA query_only=ON`. Opening a missing database must not create it; use a
properly encoded `Path.as_uri()` with SQLite `mode=ro`, including on Windows.
Close cursors/connections promptly and bound query execution; readers never run
migration, retention or checkpoints. Use parameter binding for values and fixed
allowlists for SQL identifiers. Never use `immutable=1` on a live database.
Connection handling follows the [Python 3.9 sqlite3 API](https://docs.python.org/3.9/library/sqlite3.html).

Set `wal_autocheckpoint=1000` pages initially. Any explicit PASSIVE checkpoint is
performed by the writer between batches. Record checkpoint duration and WAL size;
this threshold is not a hard WAL-size cap. Avoid forced checkpoint/VACUUM during
poll processing. Only one Hub process may own a database for writing; PR #7 must
enforce this with a cross-platform lifetime lock on an adjacent lock file, released
by the OS after a crash. An ownership conflict disables history for the second
process with a clear diagnostic; it must not steal ownership or delete the DB.

## Queue, failures and interfaces for PR #7

Use separate schema, store, projector and writer responsibilities under
`hub/history/`; exact module splitting can follow existing project conventions.
The projector returns detached immutable values, the store performs synchronous
SQLite operations, and the writer owns scheduling, the queue and diagnostics.

| Interface | Contract |
| --- | --- |
| `accept_core(status, stats, received_at_ms=...)` | Explicit acceptance result; live behavior preserved; optional immutable record only for a new accepted sample. |
| `HistoryWriter.start()` | Start asynchronous storage initialization; state is `starting` until migration/open succeeds; never hold up Fleet startup on DB I/O. |
| `HistoryWriter.submit(record)` | Nonblocking `put_nowait`; returns accepted/rejected submission, never a durability promise. No SQL or disk I/O. |
| `HistoryStore.insert_batch(records)` | Writer-thread-only atomic insertion with inserted/duplicate counts after commit. |
| `HistoryStore.prune(cutoff_ms, limit)` | One bounded deletion transaction; report committed deletion count. |
| `HistoryWriter.snapshot()` | Detached diagnostics with no DB I/O, available even if the DB cannot open. |
| `HistoryWriter.close(timeout=5.0)` | Stop acceptance, try to drain, return explicit flushed/timed-out result and remaining count. |

Start with a queue capacity of 2,048 records plus at most one 128-record in-flight
batch. Drop the incoming record on overflow and increment `dropped_queue_full`;
do not block, silently overwrite old records or grow an unbounded retry buffer.
Retry a failed batch for at most 5 seconds of monotonic elapsed time, with
interruptible backoff from 100 ms up to 1 second. After that deadline discard it
with `dropped_storage_error` accounting. Retry opening a temporarily unavailable
store at most once per 5 seconds; polling continues while the bounded queue fills
or drops. Unknown schemas/corrupt or unrelated files need operator intervention,
not automatic replacement or repeated migration attempts.

Expose internal read-only diagnostics initially: enabled flag; state
(`disabled`, `starting`, `healthy`, `degraded`, `stopped`); queue depth/capacity;
in-flight count; submitted, persisted, duplicate and dropped counters by reason;
last successful commit UTC; last error category/time; schema version; retention
and checkpoint results. Count history projection rejection and submissions after
close separately. Pending/in-flight counts distinguish accepted work from durable
work. Counters are process-local and reset at restart; logs must make that clear.
Do not expose sensitive paths, raw payloads or credentials in errors.

Storage exceptions must be caught separately from the existing core poll error
handler: a DB failure must never call `fail_core()`. If diagnostics themselves
cannot be persisted, retain them in memory and rate-limited logs. No promise of
durable outage evidence is possible while the storage device is unavailable.

## Time, discontinuities and future observations

`received_at_ms` is integer UTC Unix milliseconds on Hub's wall clock. It means
receipt, not agent collection time. Keep the agent's reported age and stale limit
unchanged; do not claim `received_at_ms - data_age_ms` is an exact collection time.
Use monotonic time for freshness, flush/retry deadlines and retention scheduling.
UTC can jump: preserve original timestamps, identify clock discontinuities and
do not use wall-clock ordering to reject sequences. Use stable identity tie-breakers
when timestamps are equal. Wall-clock rollback/forward jumps need explicit tests.

PR #8 adds separately versioned observation and gap tables, before graph APIs
promise continuity. Observation identity is
`(node_id, instance_id, group_name, observation.id)`, for storage, Wi-Fi and probe;
core sequence is never an optional publication ID. Absent, unsupported, stale and
partial groups retain their distinct meanings. Define each group's selected
fields and units in that PR; do not preempt it with unconstrained JSON storage.

Gap records are evidence intervals with node, scope (`core` or named group),
UTC bounds, generation where known, reason and whether bounds are approximate.
They contain no fabricated measurements. The required evidence rules are:

| Situation | Gap/continuity interpretation |
| --- | --- |
| Core transport fails | Hub could not reach agent; only two consecutive failures establish Offline under existing policy. |
| Agent reports stale or stats has no current sample | No current core sample; do not label it network failure. |
| Hub stops/restarts | Unobserved interval; clean shutdown can supply a bound, abrupt termination cannot establish the precise start. |
| Agent instance changes | Explicit generation boundary between observed instances; exact restart instant may be unknown. |
| Optional group stops updating | Gap applies to that group only; core may stay live. |
| Writer rejects/drops samples | Storage-loss interval when in-memory evidence survives; never infer agent failure. |
| No persisted evidence explaining missing points | `unknown`/`unobserved`; never invent Offline or interpolate. |

Recovery closes a gap at the next qualifying observation, not at an invented
sample time. An unchanged publication ID is not a fresh sample. Preserve missing
values within otherwise valid samples. Abrupt Hub/DB failures may lose both samples
and explanatory records, so reason accuracy is explicitly best effort. PR #8 must
persist Hub session/continuity metadata and storage-loss summaries once possible;
PR #7 records diagnostics but does not claim complete durable gap attribution.

PR #9 will define a separate Hub history response schema, half-open time ranges
`[from_ms, to_ms)`, a metric allowlist, a maximum 7-day query span and a maximum
1,200 returned points as initial limits. Aggregate by metric, time bucket and
continuous generation segment, retaining min/max/average and non-null sample
count. Never join across gaps or instances to satisfy a point budget. If segment
boundaries alone exceed the budget, return an explicit range-too-dense error and
require a narrower range. Nulls do not enter aggregates as zeros; empty segments
remain missing. History failure must remain independent of the live API.

## Retention and capacity budget

Adopt configurable initial defaults of 7 days for core, 7 days for observations,
30 days for events, with a retention attempt every hour on a monotonic schedule.
Only core retention exists in PR #7. Validate positive finite durations; no
unbounded-retention default. Removal from registry does not override retention.
Use receipt UTC for the cutoff and delete rows strictly older than it. Queued
records already older than the cutoff are discarded with a distinct expired
counter rather than reintroduced by a delayed writer.

Delete at most 1,000 rows per transaction, yielding to ingestion between chunks.
Each chunk is atomic; a whole sweep is resumable, not one unbounded transaction.
Index the cutoff field and report failures/last successful sweep. Deleted pages
may be reused; do not promise that the DB file immediately shrinks. No automatic
VACUUM. A clock jump can alter retention eligibility: suspend destructive cleanup
when wall-clock and monotonic elapsed time diverge by more than 60 seconds, report
`clock_discontinuity`, and require operator acknowledgement by restarting after
verifying time. Ordinary clock drift below the threshold follows receipt UTC.

The 7-day default is an initial operating policy, not measured fleet capacity.
At an ideal 5-second core cadence, one node produces at most about 120,960 rows
per week; 10/50/100 nodes imply about 1.21/6.05/12.10 million rows. Actual cadence
includes request time, jitter, failures and deduplication. At 100 nodes the ideal
20 records/second fills a 2,048-record queue in about 102 seconds without writes;
this arithmetic does not demonstrate burst tolerance or sufficient memory.

Before PR #7 capacity tuning, measure DB bytes per row including both indexes,
queue memory, commit latency, WAL growth and retention cost using synthetic
10/50/100-node workloads. Record baseline, allowed polling/API regressions and a
fixed experiment budget before each run. Include optional/event traffic when
those exist. Real Windows/Linux storage and real-device soak measurements remain
required before recommending fleet capacity; the registry's 256-node limit is
not a performance guarantee. Revisit the defaults if measurements reject them.

## Files, migration, backup and shutdown

Use `pathlib` and an explicit operator-owned local DB path outside the checkout;
resolve it at startup and log its location only to local operator logs. Require
the parent directory to exist and be writable; do not silently choose a fallback
or change working directories. An invalid CLI option fails argument validation;
an unavailable DB disables/degrades history while the live Hub remains available.
Document local filesystem permissions/Windows ACLs and account access for the DB,
lock file and SQLite sidecars. Never commit operational databases or registries.

Track schema with `PRAGMA user_version`. Version 0 is initialized only for an
empty database, never an unrelated populated file. Run ordered forward migrations
on the writer before ingestion, each with schema checks and its version increment
in the same explicit transaction. Migration failure rolls back and disables
history without destroying the old DB. Reject a newer unknown version visibly;
do not downgrade it or run partial queries against an unknown layout. PR #7 tests
0-to-1, reopen, unexpected layout, newer-version rejection and injected migration
failure; later versions add genuine upgrade fixtures.

Before changing an existing supported schema, create and verify a separate backup;
if backup fails, do not migrate. Use sqlite3's online backup API for live copies,
not a copy of the DB file alone while WAL is active. The
[SQLite backup documentation](https://www.sqlite.org/backup.html) describes this
snapshot mechanism. Restore only with Hub stopped and all handles closed; retain
the failed database for diagnosis. Rollback to old software requires restoring a
compatible backup and explicitly accepting loss of later observations. Never
delete live WAL/SHM files as a lock-recovery technique.

On shutdown, stop new history submissions and scheduling, then allow at most
5 seconds for writer drain. Report undrained work and a non-success flush result;
do not claim it persisted. The writer closes its own connection in `finally`.
A daemon writer with a bounded join prevents it alone from holding process exit;
do not close its connection from another thread. SQLite recovers committed data
after interruption; queued/uncommitted work may be lost. `busy_timeout` and join
deadlines cannot forcibly cancel a stuck filesystem call. Existing Fleet shutdown
can also wait on OS DNS: this ADR does not claim a hard bound for total Hub exit.
Windows tests must close every reader/writer handle before replacing/restoring a
DB and cover file ownership, lock release and process restart explicitly.

## Alternatives and consequences

| Alternative | Benefit | Decision |
| --- | --- | --- |
| Synchronous writes in poll workers | Less plumbing | Rejected: DB stalls would consume polling capacity. |
| One connection per poll worker | Convenient call sites | Rejected: write contention and mixed responsibilities. |
| SQLite rollback journal | Fewer sidecar concerns | Reserve for reconsideration; WAL matches concurrent query needs. |
| Store every response as JSON | Easy initial capture | Rejected: undefined field contracts and avoidable storage growth. |
| External time-series database | Mature high-volume tooling | Defer until measured limits justify installation/runtime dependencies. |
| Unbounded queue or durable spool | Larger outage tolerance | Rejected initially: unbounded memory or another storage failure domain. |

This design favors live monitoring availability over lossless delivery during
storage outages. Explicit drops, gaps and commit accounting are required parts
of that tradeoff. WAL/FULL may increase write latency on slow disks; batch and
retention settings need evidence, not assumptions. No performance or real-device
validation is claimed by this documentation change.

## Implementation and validation gates

PR #6 supplies this contract only and requires ADR review before the dependent
implementation is treated as settled. PR numbers are planned identifiers; actual
GitHub numbers may differ. Do not merge dependent work into main without explicit
user approval.

PR #7 must validate on Ubuntu/Windows and Python 3.9/3.13: accepted/duplicate/
regressing samples; instance changes; null projections; DB persistence after
restart; high-water recovery; queue overflow; DB lock/full/permission failures;
bounded retries; no poll-lock I/O; live polling during storage failure; migration
rollback; writer ownership; concurrent reads/writes; retention rollback and time
jumps; orderly and interrupted shutdown with accurate loss accounting. Use local
synthetic fixtures, fake clocks/conditions and temporary directories. Do not
inject failures into operational agents without separate authorization.

Subsequent gates are PR #8 observations and durable discontinuities, PR #9 bounded
history queries, and PR #10 graph rendering with visible gaps. Registry CLI/hot
reload, services, events, alerts, authentication and fleet benchmarks remain
separate roadmap stages. Release tags and operational deployment need separate
approval. The existing read-only endpoints, security headers, default loopback
port 9787, agent polling limits and approved SVG assets remain unchanged.
