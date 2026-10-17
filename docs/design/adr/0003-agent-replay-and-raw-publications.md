# ADR-0003: Agent replay and durable full-publication storage

- **Status:** Implemented locally; release and operational acceptance remain separate
- **Date:** 2026-10-13
- **Scope:** Opt-in recovery and persistence of complete Agent publications for historical analysis
- **Hub baseline:** `main` at `fe00919da3cc4bafc51542b0e8a56a557ed4f542`
- **Agent prerequisite:** [platmon PR #77](https://github.com/platmon/platmon/pull/77), merged as `47e384970324d71d9590c231504350ec2d40c863`; availability must be detected per Agent, not inferred from an unreleased version number
- **Related:** [ADR-0001](0001-http-pull-and-device-identity.md), [ADR-0002](0002-hub-history-storage.md), [Agent replay contract](https://github.com/platmon/platmon/blob/47e384970324d71d9590c231504350ec2d40c863/docs/reference/replay.md)

## Context

Hub already provides live fleet monitoring, selected SQLite history, events and exports.
The Agent publishes core snapshots every second by default, while Hub polls core data every
five seconds. Intermediate publications are not captured. Existing persistence projects only
selected fields, so per-core frequencies, individual sensors, power rails, network/disk I/O
and PSI cannot subsequently be recovered from the Hub database.

The Agent now offers full-publication replay through `/api/samples` and
`/api/observation-samples`. Its default memory window is at most 600 seconds and 16 MiB of
shared encoded payloads, additionally capped at 4096 records and 1 MiB per record. Capacity
can shorten retention. Agent restart clears the buffer. Replay enables bounded catch-up;
it does not guarantee complete history across outages or restarts.

## Decision

Keep existing live polling, freshness, selected history and alerts. Add an opt-in replay
collector and dedicated durable raw storage within Hub's existing SQLite writer ownership.
Require an enabled history database for raw collection; do not create an implicit database.
Use Python 3.9+ standard-library facilities and the existing explicit node registry.

Store complete validated publication payloads, their identity, schema, original reported
collection times, Hub receipt time and quality metadata. Commit a replay page's records,
unavailable ID ranges and cursor advance in one transaction. Feed AI and later historical
analysis from committed data and bounded exports, never from the live cache alone.

This extends ADR-0001's initial three-endpoint policy and ADR-0002's decisions against
whole-response storage and backfill. Those policies continue to govern the existing live
path. Only the two named, bounded replay GET resources gain access to the new path.
Implementation must update the Agent contract, repository boundaries and affected API,
configuration and architecture documentation together. No arbitrary URL fetch, Agent
control, push, discovery or `/api/history` polling is introduced.

## Separate live state from replay ingestion

Replay data may legitimately be stale or older than the live cache. It must not be passed
through `NodeState.accept_core()` or update node health, latest readings or real-time alert
state. A recovered past event must not generate a present-time threshold alert.

The current `CoreRecord` and `core_samples` schema require fresh data; the store also rejects
previously unseen IDs below the existing maximum. These are appropriate live-history rules,
but prevent backfill. Introduce dedicated replay records and tables rather than weakening
live validation or removing its regression checks. Existing selected history remains unchanged
in the first implementation; historical projections from raw records are separate follow-up work.

## HTTP and protocol contract

| Stream | Fixed resource | Publication identity |
| --- | --- | --- |
| Core | `/api/samples` | `(node_id, instance_id, core, sample.sequence)` |
| Slow group | `/api/observation-samples?group=...` | `(node_id, instance_id, group, observation.id)` |

Build `instance_id`, `after`, `limit` and `group` parameters internally with URL encoding.
Allow only `storage`, `wifi`, `battery` and `probe` groups. The browser cannot supply an
Agent URL or query. Preserve registry validation, redirect rejection and GET-only behavior.
Keep the existing 512 KiB live-response limit; allow at most 2 MiB only for replay responses.
A request returns at most 100 records and can return fewer because of its byte budget.

Validate envelope schema 1, data schema 1 for core / 2 for slow entries, expected process
identity, ascending unique IDs, nested publication-ID agreement, finite timing fields,
`next_after`, `has_more` and non-overlapping missing ranges. Records and ranges must describe
the cursor advance without unexplained holes. Unknown additive payload fields are retained;
unsupported required schema versions stop replay for that stream visibly. Do not silently
truncate a publication, skip an invalid record or advance a cursor past a rejected page.

| Result | Replay behavior |
| --- | --- |
| 200 | Validate and submit a bounded page for atomic persistence |
| 404 | Replay or that group is unavailable; retain live monitoring and report reduced capture coverage |
| 409 `instance_changed` | Record a generation boundary and start the confirmed current instance at cursor 0 |
| 409 `cursor_ahead` | Report a cursor/configuration error; do not silently reset |
| Invalid data, timeout or other HTTP failure | Keep the committed cursor and retry with bounded backoff |

Replay errors remain separate from core offline detection. Recheck process identity during
restart races; do not combine status from one generation with replay from another. Registry
changes invalidate obsolete in-flight ingestion tasks under the existing generation rules.

## Persistence and cursor durability

Add tables through an ordered, backed-up migration from the supported schema. The reviewed
baseline uses SQLite schema 5; the implementation assigns the next version independently of
Agent and Hub HTTP schema versions.

| Table | Purpose and required fields |
| --- | --- |
| `raw_publications` | Unique `(node_id, instance_id, stream, publication_id)`; data schema, full compact JSON, first Hub receipt time, reported collection times, quality and `gap_before` |
| `replay_cursors` | `(node_id, instance_id, stream)` and committed `after_id`; retained independently of raw-row pruning so expired history is not repeatedly downloaded |
| `replay_losses` | Inclusive publication-ID bounds, stream, instance, reason and first receipt time; restart evidence may have an unknown final ID |

SQLite schema 6 implements these interfaces, with an auxiliary `raw_usage` table and insert/delete triggers for transactional quota accounting. Preserve null, valid zero,
units and source identities. Raw data need not satisfy the live `data_age_ms <= stale_after_ms`
constraint. Never derive stable node identity from Agent hostname, endpoint or process ID.

A writer page transaction inserts new rows with a targeted unique-key conflict no-op, records
missing ranges idempotently and advances the cursor only after all page contents are accepted.
Duplicate retries preserve the first persisted payload and receipt metadata; replay age fields
can change between retries and do not make a publication new. On rollback, none of the page
or cursor counts as persisted. Apply cursor updates monotonically within one generation.

Extend the asynchronous writer with page-level completion acknowledgement. Queue admission
is not durable success. Schedule the next page for a stream only after its previous page's
commit acknowledgement; on failure, retry from the durable cursor. On Hub restart, load
committed cursors before ingestion. Avoid DB I/O in poll workers or inside `NodeState` locks.
A crash after commit but before acknowledgement is safe because re-ingestion is idempotent.

## Scheduling, capacity and time

Keep live polling responsive by sharing bounded node concurrency and giving live requests
priority. Catch up in round-robin bounded page turns; `has_more` must not create an unlimited
loop on one node. Apply backpressure when storage is unavailable or its queue is full.
Bound replay queue memory by encoded bytes as well as page count: the current 2048-record
queue capacity is not a suitable memory budget for pages approaching 2 MiB each.

Raw capture is disabled by default and has finite, separately configurable retention and
disk quota. Select initial numeric defaults only after measuring row/index size, JSON queue
memory, commit latency, WAL growth and cleanup cost for the intended fleet. Include retained
raw rows, loss/cursor metadata, exports and archives in capacity planning; a raw quota is not
a promise of a hard SQLite file-size cap. Keep bounded cleanup, existing WAL ownership and
clock-discontinuity safeguards. Never automatically VACUUM an operating Hub.

For scale, an assumed 10 KiB publication at 1 Hz from 100 nodes produces about 82.4 GiB of
payload per day before indexes, WAL and optional data. This is capacity arithmetic, not a
measured deployment budget. Raw retention must not inherit the seven-day summary default
without evidence that it fits the deployment.

Store Agent-reported wall times separately from Hub receipt UTC. Do not use receipt time as
the measurement time when exporting a replayed backlog. Preserve stream IDs for ordering and
restart boundaries; wall clocks can jump or differ across nodes. Any derived absolute time
must be identified as an estimate with its basis. Preserve age/staleness as of receipt and
collection-failure evidence, not an invented current reading.

Unavailable ID ranges and `gap_before` have different meanings: the former identifies lost
published samples; the latter identifies failed/too-slow core attempts that received no ID.
Neither permits zero-filling, interpolation or inventing an exact outage timestamp.

## Implementation sequence and acceptance

1. Add migration, raw-page records, transactional cursor/loss storage, writer acknowledgement,
   restart recovery and byte-bound queue behavior.
2. Add strict replay GET support, stream scheduling, page validation, fair catch-up,
   404/409 handling and coverage diagnostics. Preserve live polling and alert semantics.
3. Add bounded raw export with explicit schema, units, collection/receipt times and loss evidence.
   Define historical projections and AI access as subsequent consumers.

Validate Ubuntu/Windows and Python 3.9/3.13 with local fixtures: pagination under ongoing
publication, group-ID independence, duplicate retries, older-than-live IDs, stale records,
full metadata preservation, initial empty streams, expiration/capacity loss, oversized or
invalid pages, unsupported schemas, restart races and disabled/absent groups. Inject queue
full, commit rollback, DB failures, crash-before/after-commit and acknowledgement loss; prove
that failed pages do not advance durable cursors and replay never changes live health/alerts.
Test migration rollback, cursor retention, quotas, clock jumps, bounded exports and fair
scheduling while one node has a backlog. Set performance budgets before synthetic fleet runs;
real-device operational capacity requires separate measurements.

## Alternatives and consequences

| Alternative | Decision |
| --- | --- |
| Increase live polling frequency only | Cannot recover transport gaps or restart boundaries; retain as an independent tuning option |
| Persist only selected metrics | Keep for current monitoring, but insufficient for later feature extraction |
| Scrape Agent `/api/history` | Reject for full recovery: selected numeric series and a volatile bounded timeline |
| Reuse live acceptance/storage for backfill | Reject: freshness and regression checks intentionally exclude historical publications |
| Agent push or disk spool | Defer: adds transport or Agent durability responsibilities beyond the memory replay contract |
| External database or AI service | Defer until storage/analysis requirements justify additional dependencies |

Full raw retention enables later analysis of fields that were not selected at ingestion, but
increases storage cost and preserves hostnames, sensor labels and device metadata. Keep it
opt-in under existing local access boundaries, with explicit export and retention limits.
Agent buffer expiration and restarts still cause unrecoverable gaps. The Hub extension implements ingestion and bounded raw export. Historical projections,
AI integration, a release and deployment remain outside this change.

## Implementation notes (2026-10-17)

SQLite schema 6 implements the decision with `raw_usage` accounting triggers.
Raw retention/quota are required explicit operator choices; neither has a default.
The independent encoded queue is 8 MiB (configurable 3–64 MiB), up to 16 commands/
pages; decoded responses and allocator overhead are outside this encoded budget.
Full-envelope metadata is limited to 16 KiB. Quota exhaustion rolls back the page
and retries after backoff; it never silently evicts unexpired raw rows. Cursors
and loss metadata survive raw pruning and require separate disk capacity planning.

`GET /api/replay/status` reports capture coverage and `history.raw` reports writer
budgets/errors. The bounded local `raw-export` command supplies JSONL ZIPs; browser
raw exports, historical projections and AI access are subsequent consumers.
[Replay guide](../../guides/replay.md) documents behavior and operator choices;
[synthetic plan](../../development/benchmarks/replay-plan.md) sets pre-run budgets.
