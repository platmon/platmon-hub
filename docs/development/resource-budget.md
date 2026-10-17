# Fleet resource budget and measured limits

[Documentation index](../README.md) · [Development guide](README.md)

Evidence: [predeclared experiment plan](benchmarks/plan.md) and committed
[raw synthetic results](benchmarks/README.md), measured 2026-10-10 UTC.
Linux x86-64, Python 3.12.3, 20 logical CPUs visible to the workspace. The Hub
remains limited to 16 polling workers. CPU below is percent of one logical core,
not percent of all 20. Host resources were shared; no dedicated hardware isolation
or network emulation was claimed. Agent and HTTP-driver CPU are excluded.

## Default-setting comparison

All runs lasted 45 seconds with 5s core / 10s observations, queue 2048 and schema 4.
History was enabled. Each agent supplied a small core payload and one small probe
group. Alerts were disabled for this baseline. HTTP load was 5 fleet reads and 5
one-hour history queries per second after startup, from one sequential driver.

| Nodes | CPU / one core | Peak RSS MiB | Core interval p95 | API p95 / p99 ms | History p95 / p99 ms | Queue peak |
| --- | --- | --- | --- | --- | --- | --- |
| 10 | 0.733% | 33.320 | 5.521s | 1.058 / 2.451 | 0.990 / 1.660 | 4 |
| 50 | 1.870% | 34.133 | 5.569s | 1.438 / 1.957 | 0.986 / 1.263 | 4 |
| 100 | 3.422% | 34.605 | 5.529s | 1.773 / 1.993 | 0.963 / 1.319 | 38 |

All nodes were current, storage drop counters were zero, no HTTP requests failed,
and SQLite integrity checks passed. The 100-node API p95 was 1.68x the 10-node
baseline, within the predeclared 2x analysis threshold. All absolute targets passed.
The interval includes completion, jitter and scheduler delay; it is not exactly 5s.
These measurements do not establish the same latency for physical networks or disks.
The earlier user-supplied two-device Windows baseline belongs to a different
workload/environment and is not a regression comparator for these numbers.

## Fault and payload experiments

Each row uses 100 synthetic nodes for 15 seconds. Only Overflow changes polling
and queue defaults. Raw files include worker wait, chain duration and p50/p95/p99.

| Scenario | Current nodes | Peak RSS MiB | Queue peak | Observed outcome |
| --- | --- | --- | --- | --- |
| 10% offline | 90 | 34.414 | 0 | Healthy interval p95 5.570s; no drops |
| 10% 500ms response delay | 100 | 34.730 | 20 | Healthy interval p95 5.575s; no drops |
| 10% 2.5s response delay | 90 | 36.328 | 0 | Core timeout isolated; healthy p95 5.571s |
| Every stats response exactly 512 KiB | 100 | 118.527 | 22 | Cache memory increases materially; no drops |
| SQLite write lock for 4s | 100 | 34.488 | 198 | Writer recovers; core continues; no drops |
| Insert delay 500ms | 100 | 34.754 | 466 | Queue buffers the short delay; no drops |
| Queue 8, core 100ms, insert delay 1s | 100 | 37.668 | 8 | 10,096 queue drops reported; collection continues |
| Example alert rules enabled | 100 | 34.879 | 38 | No drops; CPU 3.683% of one core |

All fault runs had zero HTTP request failures and integrity `ok`. Normal/fault
shutdowns took less than one second except intentional Overflow (2.202s to drain).
The maximum-payload experiment covers core payload size, not simultaneously maximal
optional payloads and 32 large concurrent detail reads. Queue peaks are sampled
at 200ms and can miss shorter bursts; drop counters are authoritative.

The ten-node two-process restart experiment retained/increased core rows from the
first to the second run with the same node UUIDs and database. It had no drops,
request failures or integrity errors. Agent process lifetime remained unchanged;
separate unit tests cover agent instance/sequence restart boundaries.

## Ten-minute bounded soak

The 100-node 600.092s run retained all nodes current, 11,308 core rows and 5,891
observations, with no storage drops, HTTP failures or integrity errors. CPU averaged
6.818% of one core, peak RSS was 42.355 MiB, queue peak 132 and shutdown 0.395s.
Core interval p95/p99 was 5.556/5.571s, core chain p95/p99 8.362/11.044ms and worker
queue wait p95/p99 2.240/4.083ms. API p95/p99 was 6.109/7.101ms; History p95/p99
3.870/7.288ms across 2,892 successful requests each. Insert p95/p99 was
5.008/11.996ms. Closed DB size was 4,423,680 bytes.

The absolute targets passed, but API p95 increased from 1.773ms in the 45s run to
6.109ms, exceeding a 2x relative comparison. RSS grew 5.613 MiB after the 10s warmup,
ending at 41.910 MiB. Instrumentation retains growing (bounded) timing buffers;
SQLite caches and a longer-lived query workload also differ from the short run.
These are possible contributors, not an isolated root cause. Shared-host activity
was not controlled. The experiment budget was not extended to tune away the result;
further profiling and a longer soak must resolve the latency/memory trend before a
production stability claim. No plateau or leak-free behavior is asserted.

## Storage planning

The schema-4 storage experiment inserted 100,000 core and 50,000 small storage
observations in 9.833s (about 15,254 rows/s in this isolated test). DB size after
checkpoint was 42,270,720 bytes. Incremental storage was 421.85 bytes per core row
plus half an optional row. This excludes a realistic mix of continuity/events and
larger optional payloads; it is not the live writer's guaranteed throughput.

A 1,000-row node query aggregated to 100 points in 40.634ms within that 150,000-row
DB. The measured checkpoint took 0.447ms. Removing all 150,000 rows required 101
bounded retention calls and 1,091.024ms total. SQLite integrity remained `ok`.
These are individual cost samples, not p95 distributions. Retention frees reusable
pages and does not immediately shrink the file. Backups and WAL need separate space.

At nominal 5s core / 10s one-group observations, extrapolating this small-payload
mix to 100 nodes for 7 days gives approximately **4.75 GiB**, before operating
headroom, events, WAL and backups. This is an estimate, not a measured seven-day DB.
Three optional groups with near-16 KiB stored projections every 10s can instead
approach **277 GiB of optional payload alone** over seven days at 100 nodes. The
per-record bound is a safety ceiling, not a typical payload or capacity guarantee.
Measure real growth after installation and adjust retention before disk pressure.

## Operating recommendation

Start production evaluation at 10–50 approved devices using the unchanged defaults.
100 devices are a synthetic-tested candidate, subject to a pilot with the actual
network, payload sizes, disk, query concurrency and authentication layer. The
registry's 256-entry validation limit is not a supported-capacity claim.

For a small-payload pilot up to 100 devices, reserve at least one CPU core and
256 MiB for the Hub process, then verify actual RSS and query peaks. Consider
512 MiB or more with large optional payloads/many viewers; that reserve is a
planning recommendation, not a measured worst-case ceiling. Reserve at least twice
the measured retained DB requirement plus separately budgeted verified backups.
A nominal 10 GiB data allocation fits the small-payload 100-node estimate with
limited headroom, and is insufficient for worst-case optional payloads.

Queue 2048 handled the tested normal, 4s lock and 500ms insert-delay scenarios.
It cannot make disk outages lossless: retry/drop behavior is bounded and raw sample
rates depend on optional publication frequency and state transitions. Monitor
`/api/history/status` queue depth, drop counters, last error/commit, retention and
checkpoint timings. Lower retention or split fleets only after measuring actual
resource use; do not silently alter poll defaults to disguise saturation.

## Read-only three-device field validation

With the user's explicitly identified private registry, three enabled real agents
were read using the unchanged GET-only paths and polling defaults. The configuration
and devices were not modified. Temporary Hub processes/databases were used;
private configuration, raw field reports and screenshots are not committed.

Linux/Python 3.12.3: 60.264s collection followed by a new Hub process for 15.058s.
All three nodes remained Live/current, with zero drops. Core rows increased 36 to
44, optional rows 27 to 34; all node UUIDs matched the original unchanged registry.
History queries returned data for each device before and after restart; SQLite
integrity was `ok`. Fifteen live HTTP reads succeeded across the two processes.
An unmocked Chromium run verified all three real-device History graphs, Live states,
light/dark rendering and mobile layout without JavaScript errors.

Windows native/Python 3.13.14: 60.440s collection followed by a new Hub process for
15.263s. All three nodes remained Live/current with zero drops. Core rows increased
34 to 43 and optional rows 27 to 35. All three node UUIDs, the unchanged registry,
per-device History queries and SQLite integrity passed; fifteen live HTTP reads
succeeded. The test ran an isolated source copy without changing the installed
Windows checkout, registry or service configuration.

These short runs validate actual collection/persistence and process restart, not
hardware safety, service boot, long-term resource use or fault injection. No Agent
process was stopped, restarted or reconfigured.

## Extended 90-minute follow-up

The [extended soak](benchmarks/long-soak.md) adds 90-minute 100-node and actual
three-device Windows/Linux evidence, per-minute timelines, allocation diagnostics,
and real Dashboard checks. All collection/integrity/absolute budget checks passed.
It fixes a Windows RSS instrumentation type-cache leak and normal canceled-request
tracebacks. Synthetic RSS growth after the first hour was near-flat, while some
real working-set growth remains; the report records that analysis trigger and does
not claim a leak-free service or indefinite memory plateau.

## Remaining evidence

Before treating a candidate as production-ready, complete Windows/Linux
service reboots and live upgrade/restore tests, and a longer deployment-specific
soak. Full seven-day/max-payload history queries, many concurrent viewers, flaky
DNS/OS-level I/O stalls and storage exhaustion on the deployment filesystem remain
unverified. These synthetic results do not close those operational acceptance gates.

## Opt-in raw replay evidence

The [raw replay synthetic results](benchmarks/replay.md) and
[pre-run plan](benchmarks/replay-plan.md) cover separate schema-6 raw storage,
queue memory, 10/50/100-node initial catch-up, commit latency, WAL and bounded
cleanup. All final serial-run thresholds passed on Linux Python 3.12.3. This
in-process fixture does not establish real network/device/Windows deployment
capacity; retention and quota remain explicit operator choices without defaults.
