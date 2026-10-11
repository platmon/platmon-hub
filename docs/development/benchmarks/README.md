# Reproducing synthetic measurements

[Documentation index](../../README.md)

These files contain generated loopback-only measurements, not private registry or
operational logs. The [experiment plan](plan.md) defines targets and the time budget
before execution. Default runs use unchanged production polling/storage bounds.
The benchmark harness starts synthetic agents, an instrumented Hub and an external
HTTP load driver in separate processes. It terminates its own children and removes
temporary databases. It cannot accept arbitrary endpoints or a production registry.

```sh
python scripts/benchmark-fleet.py --nodes 10 --seconds 45 --output /tmp/normal-10.json
python scripts/benchmark-fleet.py --nodes 50 --seconds 45 --output /tmp/normal-50.json
python scripts/benchmark-fleet.py --nodes 100 --seconds 45 --output /tmp/normal-100.json
python scripts/benchmark-fleet.py --nodes 100 --seconds 600 --output /tmp/soak-100.json
python scripts/benchmark-fleet.py --nodes 100 --seconds 15 --scenario db_lock --output /tmp/lock.json
python scripts/benchmark-fleet.py --nodes 10 --seconds 10 --restart --output /tmp/restart.json
python scripts/benchmark-storage.py /tmp/storage.json
```

Use a writable temporary output path on Windows. Supported scenarios: `normal`,
`offline`, `slow`, `timeout`, `max_body`, `db_lock`, `slow_writer`, `overflow`,
`alerts`. Offline/slow/timeout affect the first 10% of synthetic nodes. Alerts use
the repository example rules. Overflow deliberately changes core interval/queue
capacity to exercise drops; it is not a default-setting capacity measurement.
SQLite/read tests use real HTTP and disk I/O, not mock response timing.

`checks` records basic validity and raises on failed integrity/healthy-node/restart
checks. Performance targets require inspecting distributions against the plan;
the script does not silently declare a supported fleet size. CI runs a 3-second
cross-platform smoke, which checks execution and isolation, not sustained capacity.
`rss_growth_after_warmup_mib` uses 10 seconds (or half-duration for short runs).
Histogram `count` is retained sample count, capped at 20,000 per metric. API
failures are reported separately, never included as successful low-latency samples.

The storage experiment uses 100,000 core rows and 50,000 small storage observations.
It measures aggregation, bounded retention transactions and checkpoint time. Its
bytes/sample estimate depends on these exact payloads/indexes and must not be used
for arbitrary optional payloads. Retention reuses pages; it does not promise that
the database file shrinks on disk.

## Explicit real-device validation

`python scripts/check-field.py --nodes /private/nodes.json --output /private/field-report.json`
uses only enabled entries in a supplied registry, starts temporary loopback Hub
processes and verifies collection/history/UUID continuity across their restart.
It never modifies agents, registry, installed services or an existing Hub. Default
workload is 60 seconds plus 15 seconds after process restart. It contacts real
registered agents with the normal fixed GET paths, so run only with authorization.
Keep field reports and screenshots outside the repository; publish a reviewed
aggregate summary, not private configuration or operational logs.

## Extended soak

[90-minute results and investigation](long-soak.md), [aggregate windows](long-soak-summary.json),
[experiment plan](long-soak-plan.md), [RSS regression proof](windows-rss-probe.json)
and [bounded-allocation/HTTP probes](windows-stdlib-probes.json). Raw field reports
and browser heap snapshots remain private.
