# ADR-0004: Downstream handoff of committed monitoring data

[Documentation index](../../README.md) · [Design reference](../README.md)

- **Status:** Accepted; stage 1 (sequence and loss accounting) implemented as SQLite schema 9, stages 2–5 pending; see [implementation notes](#implementation-notes)
- **Date:** 2026-10-17
- **Scope:** A versioned, incremental, file-based contract through which a separate post-processing system receives committed Hub data
- **Hub baseline:** `main` at `758d62f6f2b06d82b71f564d0b21c5e2914de848` (SQLite schema 7)
- **Agent prerequisite:** a platmon release that contains full-publication replay. Replay is listed under *Unreleased* in the platmon changelog; Agents running released 1.2.x answer 404 and contribute no raw publications
- **Related:** [ADR-0002](0002-hub-history-storage.md), [ADR-0003](0003-agent-replay-and-raw-publications.md), [Replay guide](../../guides/replay.md), [History exports](../../guides/data-export.md)

## Context

platmon and Hub are monitoring software: the Agent observes a host, and Hub collects,
displays, alerts on and durably records what was observed. Intended uses of the collected
data include anomaly detection, failure prediction and estimates of remaining operating time
(battery runtime, time until a disk fills, thermal margin). Labelling, feature extraction,
anonymization, model training, inference and long-term storage belong to a **separate
post-processing system** with its own repository, dependencies and release cadence. Hub keeps
its Python 3.9+ standard-library runtime and must not depend on that system.

ADR-0003 stores complete Agent publications and states that AI and historical analysis are
"subsequent consumers" of committed raw data. It does not define how a consumer receives
them. The current paths are insufficient for continuous handoff:

| Current path | Limitation for a downstream consumer |
| --- | --- |
| `raw-export` | Manual; selects by Hub receipt time; at most 1,000,000 rows and 512 MiB uncompressed per ZIP. One node-day of core publications at 1 Hz is about 1.6 GiB uncompressed on the measured host ([Capacity](#capacity)), so a single day of a single node does not fit |
| `--archive-dir` daily ZIPs | Contain the 5-second `core_samples` projection, events and gaps, not raw publications |
| Reading the SQLite file | The schema is internal (now at schema 7); a consumer reading it would break on Hub upgrades and contend with the writer |

Receipt-time windows are also not a safe incremental key. A page receives `received_at_ms`
in the poll worker before its writer transaction commits, so an export of `[t0, t1)` taken at
`t1` can miss a page received just before `t1` and committed just after; the next window
`[t1, t2)` excludes it as well. Hub wall-clock jumps can reorder receipt times. Retention and
quota pruning delete raw publications, events, gaps and Hub sessions without regard to
whether any consumer has received them, and nothing records that this happened from the
consumer's perspective.

## Decision

1. Hub produces **handoff segments**: immutable, compressed ZIP files of committed data,
   published atomically to an operator-chosen spool directory. The consumer reads only these
   files.
2. Segments are ordered by a new Hub-assigned **commit sequence**, not by receipt time.
   Each segment covers a contiguous sequence range and names its predecessor, so a consumer
   can prove it has received every committed row version or learn exactly which were lost.
3. The consumer acknowledges segments with **acknowledgement files**. Existing retention
   limits remain upper bounds, for the database and for the spool, and the raw quota remains a
   storage budget. When either deletes a row version or segment that was not acknowledged, Hub
   records a **handoff loss** for it. Quota eviction removes acknowledged raw rows before
   unacknowledged ones. Monitoring never waits for the consumer.
4. Hub hands off **observed facts only**: what the Agent published and what Hub observed about
   collection (losses, gaps, connectivity events, Hub sessions). It does not compute features,
   labels, anomaly scores or runtime estimates, and it does not anonymize.
5. Handoff is opt-in, requires `--history-db` and `--raw-capture`, and introduces no network
   listener, push client or third-party dependency.

## Responsibility boundary

| Concern | Agent | Hub | Post-processing |
| --- | --- | --- | --- |
| Signal collection (including new degradation signals such as throttling or storage health) | Owns | — | — |
| Short-term buffering | Memory replay, at most 3600 s | — | — |
| Operational storage, dashboards, alerts | — | Owns, bounded retention | — |
| Collection evidence (losses, gaps, instance changes, Hub downtime) | Reports | Records and hands off | Interprets |
| Handoff format, ordering, completeness proof | — | Owns (this ADR) | Verifies |
| Long-term storage | — | — | Owns |
| Reboot/crash classification, failure labels, human annotations | — | — | Owns |
| Device naming and registry history | — | Current registry only | Owns its own mapping |
| Normalization, features, anonymization, dataset versions | — | — | Owns |
| Anomaly detection, failure prediction, operating-time estimates | — | — | Owns |

Hub alert transitions remain Hub events and are handed off as recorded facts about Hub's
rules; they are not ground-truth labels.

## Segment contents

Every segment contains the row versions committed in its sequence range:

| Member | Source | Notes |
| --- | --- | --- |
| `raw_publications.jsonl` | `raw_publications` | One row per publication; `envelope_digest` replaces the shared envelope |
| `raw_envelopes.jsonl` | `raw_envelopes` | Each envelope referenced by the segment, once |
| `replay_losses.jsonl` | `replay_losses` | Unavailable Agent publication ranges; a range extended later appears again |
| `gaps.jsonl` | `gaps` | Inserted gaps and later versions with a new end |
| `events.jsonl` | `events` | Connectivity, state, restart and alert transitions |
| `hub_sessions.jsonl` | `hub_sessions` | Hub start, last-seen and stop evidence, so Hub downtime is distinguishable from Agent loss |
| `handoff_losses.jsonl` | new | Row versions and segments deleted before acknowledgement; see below |
| `nodes.jsonl` | registry snapshot | `node_id`, `enabled` and the per-stream replay coverage reported by `/api/replay/status`; no names or endpoints |
| `manifest.json` | — | See below |

`core_samples` and `observations` are projections of raw data and are not handed off. A node
whose Agent lacks replay still contributes gaps, events and Hub sessions, and `nodes.jsonl`
shows its streams as unsupported, so the consumer can tell missing capability from missing
data. Upgrading the Agent was the only way to obtain its measurements through handoff;
[ADR-0007](0007-sampled-capture-level.md) supersedes this with sampled capture from live polls.
`continuity` and `replay_cursors` are Hub working state; their effects reach the consumer
through gaps, losses and publication IDs.

Registry names and endpoints are excluded. They are operator-assigned labels that change
independently of the data, and Agent payloads already carry the host's own identity; the
consumer keeps its own `node_id` mapping.

Rows keep the ADR-0003 time semantics: Hub receipt UTC milliseconds, Agent-reported Unix
seconds that may jump, and elapsed durations in milliseconds remain separate fields. Nulls,
valid zeros, units and unknown additive Agent fields are preserved verbatim.

### Manifest

`manifest.json` records at least: `format` (`platmon-hub-handoff`), `format_version` (1),
`hub_store_id`, `segment` name, `from_seq` (exclusive), `to_seq` (inclusive),
`previous_segment` and its SHA-256, Hub version, SQLite schema version, Agent payload schemas,
the units and timestamp descriptions already used by `raw-export`, per-member row counts and
SHA-256 hashes, the receipt-time bounds of the contained rows, and the creation time.

`hub_store_id` is a UUID generated when the history database is created. A restored backup
or a second Hub has a different store identity or a sequence that does not continue the
chain, so a consumer never silently merges two histories.

## Ordering: commit sequence

Add a store-wide, strictly increasing `handoff_seq`. While handoff is enabled, the writer
assigns the next value to a row in the same transaction that inserts it or changes it in a
way the consumer must see. Hold the next value in a single-row counter table so values are
never reused after deletes; SQLite rowids are not sufficient because they may be reused.
Because one writer commits transactions serially, a read snapshot that contains sequence `S`
contains every smaller committed value.

The unit of delivery is a **row version**: each assigned sequence value is delivered in
exactly one segment, and a row changed after delivery is delivered again with its new value.

| Table | New sequence value on | Consumer key (latest version wins) |
| --- | --- | --- |
| `raw_publications` | insert only (duplicates are no-ops) | `(node_id, instance_id, stream, publication_id)` |
| `replay_losses` | insert, and `to_id` extension | `(node_id, instance_id, stream, from_id, reason)` |
| `gaps` | insert, and any change of `end_ms`, including pruning truncation | `gap_uid` |
| `events` | insert only | `event_id` |
| `hub_sessions` | insert, close, and `last_seen_ms` change at most once per segment | `session_id` |
| `handoff_losses` | insert, and range extension | `loss_id` |

`gaps.id` is an `INTEGER PRIMARY KEY` without `AUTOINCREMENT`: once retention deletes every
gap, the next gap reuses id 1, which would overwrite an unrelated gap the consumer already
holds. The migration therefore adds a `gap_uid` UUID column, filled for existing rows, and the
writer sets it on insert. `handoff_losses.loss_id` is a UUID for the same reason. Hub session
IDs and event IDs are already random UUIDs.

`hub_sessions.last_seen_ms` currently changes in every core commit. The writer assigns it a
new sequence value only when that row's current value is at or below `delivered_seq` (see
below), so each segment carries at most one version per session.

A segment selects `from_seq < handoff_seq <= to_seq`, where `to_seq` is at most the largest
value visible in the segment's read snapshot and may be smaller when the size limit closes
the segment earlier. The next segment starts at `to_seq`.

## Durable handoff state and recovery

A single-row `handoff_state` table, changed only through the writer, holds `delivered_seq`,
`acked_seq`, the last published segment's name and SHA-256, the configured start choice and
backfill bound, and whether handoff is enabled. Quota eviction order, loss recording and the
session-version rule read these values inside the writer transaction, so their behaviour does
not depend on rescanning files after a restart.

1. The segment builder publishes a file, then submits a writer command that advances
   `delivered_seq` and the chain head. A crash between the two leaves a published file that the
   state does not yet name.
2. On startup, before producing new segments, Hub lists `<spool>/<hub_store_id>/`. A segment
   whose range starts at `delivered_seq` and whose manifest hash and predecessor match the chain
   head is adopted and the state advanced. Any other unexpected file stops handoff with a
   visible error rather than being overwritten or skipped.
3. Acknowledgement files are read off the writer thread; a valid contiguous prefix is applied
   as one writer command that advances `acked_seq`.

## Spool, publication and acknowledgement

- Segment file: `<spool>/<hub_store_id>/<from_seq+1>-<to_seq>.zip`, written to a temporary file
  in the same directory and published with a hard link, as the existing exports do. Existing
  files are never overwritten. Members use ZIP deflate.
- A segment closes after **15 minutes** or **256 MiB of uncompressed members**, whichever comes
  first, or on clean shutdown. With the measured payload the time limit governs below about
  fifteen nodes ([Capacity](#capacity)).
- The consumer acknowledges a segment by writing
  `<spool>/<hub_store_id>/acks/<segment>.ack` containing the segment's SHA-256. It writes a
  temporary file in the same directory and renames it, so Hub never sees a partial
  acknowledgement. File acknowledgement is the only mechanism in format 1; a loopback HTTP
  acknowledgement is reconsidered only together with a pull API.
- Hub reads acknowledgements off the writer thread. A file whose content does not match the
  segment's hash is ignored, reported in status and checked again later; it never advances
  the acknowledged sequence.
- Acknowledgement is cumulative only across a contiguous chain: the acknowledged sequence is
  the end of the longest prefix of acknowledged segments.
- Segment generation runs off the writer thread on a read-only connection, as exports do, and
  never blocks live polling or history commits.
- `--handoff-spool`, `--handoff-spool-quota-mib` and `--handoff-spool-retention-days` are
  required when handoff is enabled and have no defaults, like the raw retention and quota.
  Startup fails when the spool is missing, not writable, on a filesystem without hard links,
  or when spool retention exceeds raw retention. After startup, spool errors are reported in
  status and retried with bounded backoff; they never stop collection.

### Spool deletion

The spool obeys the same upper bounds as the database:

- An acknowledged segment is deleted after the spool retention period.
- Any segment, acknowledged or not, is deleted once its newest contained receipt time is older
  than the shortest retention that applies to its members. A deleted unacknowledged segment
  produces a `handoff_losses` row with reason `retention` naming the segment and its range.
- When the spool quota is reached by unacknowledged segments, Hub stops producing segments and
  reports the lag; it does not delete unacknowledged segments to make room.

Shared-directory and network-filesystem semantics (hard links, atomic rename visibility) are
the operator's responsibility; the reference deployment is a local directory read by a
consumer on the same host or synchronized by an external tool.

## Interaction with retention and quota

Monitoring availability and the operator's stated limits take priority over handoff
completeness. Handoff never extends how long Hub keeps data and never raises a storage
budget; it guarantees only that every deletion of an undelivered or unacknowledged row
version is accounted for.

1. **Retention stays an upper bound.** `--raw-retention-days`, `--history-retention-days` and
   `--event-retention-days` delete rows as today, acknowledged or not. This preserves the
   documented privacy and capacity meaning of retention.
2. **Quota eviction prefers acknowledged raw rows.** When the raw quota is exceeded, Hub evicts
   acknowledged raw publications oldest first, then unacknowledged ones oldest first. The quota
   remains the only raw storage budget; there is no separate handoff budget to reconcile with it.
3. **Every deletion of a row the consumer has not acknowledged is recorded.** In the same
   transaction as the delete, Hub inserts or extends a `handoff_losses` row with the table, the
   deleted sequence range, the reason (`retention`, `quota` or `not_handed_off`) and the
   receipt-time bounds. This covers raw publications, replay losses, gaps, events and Hub
   sessions with `handoff_seq > acked_seq`, and rows still awaiting backfill (`handoff_seq` is
   null and receipt time is within the backfill bound), which are identified by receipt-time
   bounds instead of a sequence range.
4. **Loss records are coalesced and bounded.** A deletion that continues the previous loss of
   the same table and reason extends that row instead of inserting another, so a consumer
   outage produces a few rows per table rather than one per pruning batch. Loss rows are kept
   until acknowledged and then follow event retention. While handoff is disabled no losses are
   recorded.
5. **A full spool stops segment production, not collection.** Rows continue under rules 1–4
   and the resulting losses are delivered once segments resume.

Operators who need a longer consumer outage tolerance raise retention and quota explicitly
and size the disk for them.

## Capacity

Measured on one x86_64 PC (22 logical CPUs, 58 `sensor_meta` entries) with the current Agent
and this Hub branch, over a four-minute run with every default observation group enabled.
Other hosts must be measured; a Jetson or Raspberry Pi has fewer CPUs but may report more
power rails and thermal zones.

| Quantity | Measured | Per node-day at 1 Hz |
| --- | --- | --- |
| Core publication, stored `encoded_bytes` | 19,628 B average over 268 publications | 1.58 GiB |
| SQLite file vs encoded payload | 5.89 MB file for 5.35 MB payload (+10 %) | about 1.7 GiB on disk |
| Slow groups (storage, Wi-Fi, battery, probe) | under 2 % of payload | included above |
| Deflate of consecutive core publications | 1,165,065 → 67,231 B for 60 rows (17×) | about 93 MiB per node-day in segments |

`sensor_meta` is 13.5 KB (69 %) of each compactly re-serialized core publication. Its per-read timing (`read`
offsets, 4.4 KB) changes every sample, but the remaining 9.9 KB (51 % of the publication)
took only two distinct values in 268 samples. Compression in segments removes most of that
repetition; the SQLite raw table does not. Deduplicating the static part in storage, as
`raw_envelopes` already does for envelopes, would roughly halve database growth while keeping
payloads reconstructable byte for byte. It changes ADR-0003 storage and is left to a separate
decision.

### Storage levels

Storage cost is driven by keeping uncompressed 1 Hz raw data in SQLite for days, not by the
1 Hz data itself. Data is therefore kept at levels with different resolution and retention,
split along the responsibility boundary. Sizes are per node-day on the measured host; L1 is
estimated from the 234 numeric leaves of a core publication (CPU 66, network 44, disk I/O 38,
temperature 28, pressure 22 and others), the rest are measured.

| Level | Content | Per node-day | Purpose |
| --- | --- | --- | --- |
| L0 raw, SQLite | Every published snapshot, 1 Hz | 1.7 GiB | Handoff source, short-term inspection |
| L0 raw, compressed | The same, as deflated segments | about 93 MiB | Consumer's copy of the original data |
| L1 aggregate | min, mean, max and last of each numeric leaf per minute | about 15 MiB uncompressed (estimate) | Long trends, failure prediction, operating-time estimates |
| L2 projection (`core_samples`) | 20 selected columns, 5 s | about 3.6 MiB | Dashboard, alerts |
| L3 summaries (`--archive-dir`) | Daily and monthly reports | small | Operational reporting |

Hub keeps only what monitoring needs:

| Hub level | Retention | Per node |
| --- | --- | --- |
| L0 raw | at most 24 hours: the longest tolerated consumer outage, not the analysis window | up to 1.7 GiB |
| L2 projection | 7–30 days | 25–110 MiB |
| L3 summaries | as configured today | small |

The consumer owns long-term storage. This ADR does not bind its design; the intended starting
point is:

| Consumer level | Retention | Per node |
| --- | --- | --- |
| L0 compressed raw | 30–90 days | 3–8 GiB |
| L1 aggregate | a year or more | under 5.5 GiB per year before columnar compression |
| L0 windows around labelled events | indefinitely | about 1.3 MiB per 20-minute window, compressed |

L1 statistics and labelled-event windows belong to the consumer: which statistics serve an
analysis, and which periods are failures, are processing decisions, and only the consumer
holds the labels. Hub therefore neither computes L1 nor pins raw data around its own events.

Hub planning figures with 24-hour raw retention, before WAL, backups, exports and archives:

| Fleet | SQLite raw (24 h) | Spool per day (compressed) |
| --- | --- | --- |
| 1 node | 1.7 GiB | 0.1 GiB |
| 10 nodes | 17 GiB | 1 GiB |
| 100 nodes | 170 GiB | 9 GiB |

The raw quota is sized from this table, and the spool retention is at most the raw retention.
Two further reductions are separate decisions: compressed raw payload storage
([ADR-0006](0006-raw-payload-compression.md), about 0.2 GiB instead of 1.7 GiB per node-day on
the measured host; it replaced the `sensor_meta` deduplication first considered here), and a
per-node capture level ([ADR-0007](0007-sampled-capture-level.md)) that stores the full documents
Hub already polls every five seconds for nodes that need neither 1 Hz data nor outage recovery,
which also works with Agents that lack replay.

## Status and diagnostics

Extend `GET /api/history/status` (or add `GET /api/handoff/status`) with: enabled state,
store identity, last committed, delivered and acknowledged sequence, unacknowledged segments
and bytes, unacknowledged raw bytes against the raw quota, the oldest unacknowledged receipt
time and its distance from each retention limit, nodes without replay coverage, rejected
acknowledgements, handoff losses by table and reason, and the last error. The dashboard warns
before unacknowledged data reaches a retention limit or the quota.

## Consumer obligations

The contract also binds the consumer. It must:

- verify member hashes and the `previous_segment` chain before acknowledging;
- write acknowledgements atomically (temporary file and rename in the same directory);
- apply row versions by the keys in the ordering table, keeping the version with the larger
  `handoff_seq`;
- reject an unknown major `format_version` and ignore unknown additive fields;
- never infer measurements across `replay_losses`, `handoff_losses`, gaps or Hub downtime;
- never read the Hub SQLite database directly.

## Compatibility and versioning

`format_version` is independent of the SQLite schema, Hub HTTP schema and Agent schemas.
Additive members or fields keep the version; removed or reinterpreted fields require a new
version. Hub writes one format version at a time. Agent payloads are passed through with
their own schema numbers, so new Agent fields reach the consumer without a Hub change.

The migration adds a nullable `handoff_seq` column and index to each handed-off table, the
`gap_uid` column, the counter, `handoff_state` and `handoff_losses`, and generates
`hub_store_id`. It does not number existing rows. Sequence values are assigned only while
handoff is enabled. When handoff is enabled, the operator chooses where it starts:

| Start | Behaviour |
| --- | --- |
| `now` (default) | Existing rows are not handed off. Hub records one `not_handed_off` loss per table with the receipt-time bounds of those rows, so the consumer knows the earlier history exists and was excluded |
| `existing` | The current receipt time is stored as the backfill bound. A bounded background task assigns sequence values to unnumbered rows within the bound in a fixed order (receipt time, node, instance, stream, ID), in batches outside the migration transaction. Deletions of rows still awaiting backfill are recorded under rule 3 |

Disabling handoff stops numbering. Enabling it again records a `not_handed_off` loss per table
for the rows committed while it was disabled, then continues the same chain. Keeping
numbering out of the migration avoids rewriting a multi-gigabyte raw table inside the
backed-up schema upgrade. The chosen start is recorded in the first segment's manifest.

## Privacy

Segments contain hostnames, models, sensor labels and probe target addresses inside Agent
payloads. Hub does not remove them, because the consumer may need them and anonymization is a
processing decision. The spool inherits the protection expected of the history database and
is documented as private operational data. Because retention stays an upper bound for the
database and the spool, handoff never makes Hub retain this data longer than configured.

## Non-goals

- Features, aggregates, labels, annotations, anomaly scores or predictions in Hub.
- Handing off `core_samples` or `observations` for Agents without replay.
- Parquet, Arrow or other formats requiring third-party libraries.
- Network push, a streaming API or remote acknowledgement endpoints.
- Long-term storage in Hub, or retention extended on the consumer's behalf.

## Alternatives

| Alternative | Decision |
| --- | --- |
| Scheduled `raw-export` by receipt-time window | Reject as the contract: misses rows committed after the window closes and is not idempotent across clock jumps; acceptable as a stopgap with overlapping windows and consumer-side de-duplication |
| Consumer reads SQLite | Reject: couples the consumer to an internal schema and the writer's locking |
| HTTP pull API for the consumer | Defer: suits near-real-time detection, which the first goals do not need; may later serve the same segments |
| Hub pushes to an external store or message queue | Reject for now: adds credentials, dependencies and delivery retries to monitoring software |
| Hub computes features or downsampled tables | Reject: processing choices change faster than the monitoring contract |
| Hand off projections for Agents without replay | Reject: 5-second selected fields do not serve the intended analyses and would double the format and its tests; upgrade the Agent instead |
| Protect unacknowledged rows or segments from retention or quota | Reject: lets an absent consumer exceed the operator's retention and disk limits |
| Block monitoring until the consumer catches up | Reject: an absent consumer must not stop collection |

## Consequences

The post-processing system can be developed, restarted or replaced independently. For every
handed-off table it receives either each committed row version or a loss record identifying
what was deleted and why. Hub gains a migration, a counter and handoff state, loss accounting
in its pruning paths, a segment builder, a spool owner with startup recovery and diagnostics.
Operators must size retention, quota and spool for their expected consumer outages and run a
consumer that acknowledges segments; otherwise the spool fills and losses accumulate. Nodes
must run a replay-capable Agent before their measurements reach the consumer.

## Resolved questions

| Question | Decision |
| --- | --- |
| Projections for Agents without replay | Not handed off; a replay-capable Agent release is a prerequisite |
| Registry names in `nodes.jsonl` | Excluded; the consumer owns naming |
| Acknowledgement mechanism | Files only in format 1 |
| Segment limits, spool quota and retention | 15 minutes / 256 MiB uncompressed; spool quota and retention required without defaults |
| Unwritable spool | Refuse to start; runtime errors are reported and retried |

## Implementation sequence and acceptance

1. Migration: `hub_store_id`, nullable `handoff_seq` columns and indexes, `gap_uid`, counter,
   `handoff_state` and `handoff_losses`; sequence assignment in every insert/update path listed
   above while enabled.
2. Loss accounting and coalescing in raw retention, quota eviction and history/event pruning,
   including rows awaiting backfill, and quota eviction order (acknowledged first).
3. Segment builder and CLI (`python -m hub handoff --once`) producing format 1 from a range.
4. Background scheduler, startup validation and recovery, spool ownership and deletion,
   acknowledgement validation, the start choice and the `existing` backfill task.
5. Status and dashboard diagnostics, documentation of retention/quota interaction, and a
   reference consumer test that verifies chains and hashes and acknowledges segments.

Acceptance covers: rows committed during segment generation appear in exactly one segment;
updated gaps, replay losses and sessions appear as later versions; gap identity survives id
reuse after all gaps are pruned; Hub crash before and after publication and after publication
but before state advance; unexpected spool files; duplicate, partial, tampered and missing
acknowledgements; retention and quota deletions of unacknowledged rows in every handed-off
table, and of rows awaiting backfill, producing coalesced handoff losses while capture
continues; acknowledged rows evicted before unacknowledged ones; spool full; spool retention
deleting unacknowledged segments with a loss; disable and re-enable; clock jumps; database
restore producing a new chain; Agents without replay; both start choices, including backfill
timing on a raw table of the measured production size; segment and SQLite growth measured on a
Jetson and a Raspberry Pi; Windows and Linux; every interpreter supported under
[ADR-0005](0005-os-and-python-support.md).

## Implementation notes

**Stage 1 (2026-10-17), SQLite schema 9.** Sequence values are assigned by `AFTER INSERT` and
`AFTER UPDATE OF <column>` triggers that consult `handoff_state.enabled`, so every write path
is covered without per-call changes; nothing is numbered while handoff is disabled. Updated
versions are created for a changed gap `end_ms` (including pruning truncation), an extended
replay-loss `to_id`, a Hub session's `closed_at_ms`, and its `last_seen_ms` only once its
current version is at or below `delivered_seq`. `gap_uid` and `loss_id` are 32 hexadecimal
characters from a random 128-bit value, like Hub session IDs. `handoff_state` already holds the
delivered and acknowledged sequence, the last segment and the start choice, so the later
stages need no further schema change for them.

Deletions in retention pruning, raw retention and raw quota eviction select their rows first
and record the unacknowledged ones, or rows still awaiting an `existing` backfill, in the same
transaction. A deletion extends the table's current unacknowledged loss for that reason
instead of adding a row. Quota eviction removes acknowledged raw rows first, in sequence order,
and only then the oldest rows. `enable_handoff('now')` records one `not_handed_off` loss per
table for rows not numbered since the previous enable. The segment builder, spool,
acknowledgement, backfill task, CLI options and diagnostics are stages 2–5.
