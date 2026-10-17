# Changelog

## Unreleased — Agent replay and raw persistence

- Add opt-in full core/slow publication replay with bounded fair catch-up, per-stream
  coverage diagnostics and isolation from Live monitoring and alerts.
- SQLite schema 6 atomically commits raw pages, unavailable ranges, restart evidence
  and durable cursors; acknowledge only committed pages and safely retry duplicates.
- Require explicit raw retention/quota; bound the encoded ingress queue separately,
  preserve cursor/loss metadata through raw pruning, and pause on quota/storage errors.
- Add `raw-export` for bounded consistent JSONL ZIP exports with original collection
  times, receipt times, schemas, units and loss evidence. Existing CSV archives and
  reports remain summary consumers.

## Unreleased — platmon 1.2.0 compatibility

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
