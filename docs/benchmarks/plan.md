# Synthetic experiment plan (2026-10-10)

[Documentation index](../README.md)

Baseline: current merged PR #16, local Linux x86-64 / Python 3.12.3, 10 synthetic
agents, default 5s core / 10s optional polling, 16 workers, queue 2048, schema 4.
Compare 50 and 100 agents with identical settings and 45-second runs. API load:
5 fleet and 5 one-hour history reads per second after a 1-second startup warmup.
Agent process and HTTP load driver are separate from the measured Hub process.
Hub process includes small sampling/instrumentation overhead. No real devices.

Targets: all healthy nodes current, zero normal storage drops, SQLite quick_check
ok; normal poll-start interval p95 <= 7.5s, Fleet API p95 < 100ms, History p95 < 250ms,
normal Hub RSS peak < 128 MiB. Do not regress 10-node p95 by >2x without analysis.
Stop expansion for corruption, stalled shutdown or lost healthy-node progress.
These are experiment targets, not established product service-level guarantees.

Budget: approximately 15 minutes of measured workload, plus bounded startup/checks.
Run 10/50/100 x45s, one 100-node 600s soak, seven 100-node fault/payload scenarios
x15s, one 100-node example-alert-rules run x15s, and a 10-node 2x10s restart check. No parameter tuning search. Reassess if a
target fails; report evidence rather than repeatedly adjusting until it passes.

Fault scenarios: 10% dropped connections, 10% 500ms delay per request, 10% 2.5s delay
(core timeout is 2s), exact 512 KiB stats, a 4s SQLite write lock, 500ms insert delay,
and deliberate queue saturation. Overflow is explicitly a different stress setup:
100ms core interval, queue 8, 1s insert delay. Normal default capacity is not inferred
from that stress run. Healthy nodes must continue and the Hub must terminate cleanly.

Resource samples every 200ms; duration/queue histograms retain at most 20,000
measurements. Latencies are nearest-rank p50/p95/p99. RSS is current process working
set (`/proc/self/statm` on Linux, GetProcessMemoryInfo on Windows). CPU is process
CPU divided by elapsed wall time, as percent of one core. DB size is after close;
writer throughput counters include continuity/optional/event records, not just core
rows. HTTP measurement excludes the final 400ms shutdown boundary. Failure counters
must still be examined. Short-run p99 and checkpoint/retention timing have few samples.

A 10-minute run is a bounded soak, not proof of 24/7 stability or a leak-free system.
Actual 3-device, Windows/Linux reboot and production long-duration tests remain
separate and require authorized environments. Do not claim 256-device capacity.
