# Changelog

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
