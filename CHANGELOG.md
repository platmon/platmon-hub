# Changelog

## Unreleased

- Prepare downstream handoff (ADR-0004 stage 1, SQLite schema 9): a store identity, a
  commit sequence assigned by triggers only while handoff is enabled, `gaps.gap_uid`, and
  `handoff_losses` recording retention and quota deletions of unacknowledged rows. Quota
  eviction prefers acknowledged rows once handoff is enabled. Handoff cannot be enabled
  from the command line yet; with it disabled, storage behaves as before. Upgrading takes a
  verified schema-8 backup.

- Add a per-node raw capture level (ADR-0007): registry `capture` is `auto` (default),
  `replay`, `sampled` or `off`, set with `platmon-hub nodes set-capture`. Sampled capture
  stores the `/api/stats` and `/api/observations` documents Hub already polls under the
  replay identity, marked `source: live_poll`, so Agents without replay (platmon 1.2.x)
  contribute full measurements under `auto`. `/api/replay/status` reports each node's level
  and each stream's source. The default level is not written to registry files.
- Correct the Agent contract: with raw capture Hub may hold a replay request beside the live
  request sequence, two connections per node at most.

- Store new raw publications compressed (ADR-0006, SQLite schema 8): `zlib` with a
  shared, reference-counted dictionary per stream, verified to decode to the identical
  JSON before commit, with JSON as the fallback. The raw quota charges stored bytes; on
  one x86_64 host stored payload was 6.4 % of its JSON size, about 185 MiB per node-day
  instead of about 1.7 GiB. Existing rows stay JSON. `raw-export` output is unchanged.
  `history.raw` reports compression counters. Upgrading takes a verified schema-7 backup;
  a downgrade needs that backup.

## 1.0.0 — 2026-10-17

First release. From this version the Hub HTTP API (schema 1 routes and fields), the
`platmon-hub` command's options and subcommands, the registry and alert-rule JSON
formats and the export formats follow MAJOR.MINOR.PATCH: a breaking change needs a
new major version ([release policy](docs/development/release.md#versioning)).
Supports platmon Agents 1.0.0–1.3.0. The `0.2.0rc1` candidate below was never tagged
or published; 1.0.0 includes it and the changes listed here.

### platmon 1.3.0 compatibility

- Declare platmon 1.0.0–1.3.0 support. 1.3.0 is additive for Hub: live schemas are
  unchanged, the new `/api/status` battery and Wi-Fi `levels` are ignored, and the
  released replay endpoints keep the contract raw capture already uses. The Agent
  contract is pinned to tag v1.3.0, with a synthetic 1.3.0 compatibility test.
- Correct the export guide: schema 7 stores busiest-interface and disk I/O rates.

### no backups of a new database

- Creating a History database no longer leaves a verified backup of each empty
  intermediate schema (`history.db.v1-backup-*` to `v6-backup-*`). Upgrading an
  existing database still backs it up before every migration.

### NPU and probe alerts

- Add `npu_percent` and `probe_failure_percent` alert conditions. The probe condition
  is the worst target's failed share of its recent TCP connect attempts; it follows the
  probe group's own freshness and is unknown when the group is stale or has no target.
- Node summaries gain an additive nullable `probe_failure_percent` field.

### pressure alerts

- Add `psi_cpu_some_avg10`, `psi_memory_some_avg10` and `psi_io_some_avg10` alert
  conditions from the Agent's pressure stall information, following core freshness.
  Node summaries gain an additive `pressure` object with the same three values.

### battery and Wi-Fi alerts

- Add `battery_percent` and `wifi_signal_dbm` alert conditions, where the low reading is
  the bad one: `warning >= critical`, compared at or below, recovering above warning plus
  hysteresis. They use the lowest present battery charge and the weakest connected signal
  from current observations. Node summaries gain both as additive nullable fields.

### pressure and I/O rate history

- History schema 7 adds nullable CPU/memory/I/O pressure (`some avg10`), busiest-interface
  network receive/send rates and summed physical-disk read/write rates to `core_samples`,
  with a verified pre-upgrade backup. Existing rows keep NULL.
- The History API, dashboard History chart, CSV export and report summaries include them.
  Daily summaries move to `summary_format_version: 3`; formats 1 and 2 remain readable.

### Agent replay and raw persistence

- Add opt-in full core/slow publication replay with bounded fair catch-up, per-stream
  coverage diagnostics and isolation from Live monitoring and alerts.
- SQLite schema 6 atomically commits raw pages, unavailable ranges, restart evidence
  and durable cursors; acknowledge only committed pages and safely retry duplicates.
- Require explicit raw retention/quota; bound the encoded ingress queue separately,
  preserve cursor/loss metadata through raw pruning, evict the oldest raw rows under
  quota pressure, and pause on storage errors.
- Add `raw-export` for bounded consistent JSONL ZIP exports with original collection
  times, receipt times, schemas, units and loss evidence. Existing CSV archives and
  reports remain summary consumers.

### platmon 1.2.0 compatibility

- Consume the additive platmon v1.1.0/v1.2.0 fields while retaining v1.0.0
  compatibility: status/stats schema 1 and observations schema 2 are unchanged.
- Show GPU/NPU driver, provider, usage and clocks, including detected devices
  without readings. Add NPU usage to node summaries, History, CSV exports and
  daily/monthly reports; missing readings remain null and idle zero remains zero.
- Add Power & battery details: power rails, fans, battery presence/charge/status,
  charge or energy capacity, full/design ratio, voltage, signed current/power,
  cycle count and external power's true/false/unknown state. Battery freshness
  follows its own observation, independent of core samples.
- Persist bounded battery observations with deduplication, continuity and optional
  error/recovery events; include them in opt-in observation exports and archives.
- History schema 5 adds nullable `npu_percent`, with a verified pre-upgrade backup.
  Daily summary format 2 includes NPU statistics; monthly reporting still accepts
  format 1 summaries, treating their NPU readings as unknown. Existing benchmark
  evidence remains evidence of schema 4, not a new schema-5 capacity measurement.

## 0.2.0rc1 — candidate (2026-10-11)

This candidate is prepared in source and CI artifacts. It has not been tagged,
published as a release or deployed by this work. Operational acceptance gates are
listed in [release policy](docs/development/release.md).

- SQLite history with a bounded asynchronous writer, deduplication, retention,
  independent observations, restart/gap evidence and migrations through schema 4.
- Bounded read-only History and Event APIs; local Canvas time-series graphs with
  missing data, extrema, generation boundaries and light/dark/mobile support.
- Local atomic registry CLI and validated hot reload preserving node UUIDs.
- Wheel packaging, backup command, rotating logs and opt-in Windows Task Scheduler /
  Linux systemd examples with separated configuration and state directories.
- Persistent transition events and sustained internal alerts with hysteresis,
  recovery, cooldown and conservative restart behavior.
- Optional tested TLS/auth proxy, HTTP admission bounds, loopback Host protection
  and redacted access logs. No writable HTTP administration or Agent control.
- Fixes: an Agent without `/api/observations` (404) or with an unknown observations
  schema is reported as unsupported/incompatible and no longer raises
  `optional_error`; an unusable `--nodes` file exits with a short explanation
  instead of a traceback; a web rename keeps the stored endpoint spelling
  (host case, implicit `:80`) instead of recording an endpoint change.
- Fixes: `--bind ::1` and other IPv6 literals work; a foreign or unresolvable bind
  address is explained instead of a traceback; a rejected write whose body arrives
  late no longer loses its response to a TCP reset on Windows; registry saves
  retry briefly while another program holds the file open on Windows; `HEAD` is
  answered on read-only routes (not on downloads).
- Dashboard: device cards keep keyboard focus across refreshes; Enable/Disable
  applies directly; status times are shown in UTC like the rest of the page; the
  Data tab's default range stays "last 24 hours" until edited.
- Synthetic 10/50/100-node evidence, a bounded ten-minute soak and fault tests;
  real three-device read-only checks on Linux and native Windows.

Compatibility: Python 3.9+ standard-library runtime; fixed default loopback 9787;
Agent schemas unchanged (status/stats 1, observations 2). Hub HTTP schema remains
1 with additive endpoints/fields. History now uses schema 4; downgrading code
requires a compatible backup, never an in-place schema downgrade. Custom loopback
Host names need proxy rewrite. See the resource budget for unresolved longer-soak
latency/memory trends and the security guide for authentication/session limits.

## Original Fleet Dashboard baseline

Commit `bdb77b5398751ad34c91711bbbe02191a5839f87`: explicit registry, independent
bounded HTTP Pull, current/last-known state and branded read-only Dashboard.
Existing two-device Windows field evidence remains documented separately and was
not reclassified as validation of the new persistence features.
