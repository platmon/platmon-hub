# Extended soak plan (2026-10-11 KST)

[Documentation index](../../README.md) · [Development guide](../README.md)

Deadline: at most 09:00 KST on 2026-10-11; finish earlier when the experiment and
any necessary fix/recheck provide sufficient evidence. Baseline is merged
`0.2.0rc1`, main `b4c3b26541aef182197b1e12bba877d0cc592437`.
Existing ten-minute 100-node evidence showed rising RSS/API latency; investigate
rather than assume a runtime leak or claim long-term stability.

## Workload and measurement

Initial budget: one continuous 100-node, 90-minute synthetic run and concurrent
90-minute read-only three-device runs on Linux and native Windows. Default polling,
16 workers, queue 2048, history enabled, small synthetic payloads. Synthetic load:
five fleet and five rotating-node one-hour History reads per second. Real-device
load: one fleet and one rotating-node History read per second. Temporary Hub ports,
databases and source copies; existing services, registry and Agents stay unchanged.
Field reports and operational identifiers remain private.

One-minute windows report process CPU (percent of one core), RSS, queue/counters,
poll chain/interval/worker/insert timing and HTTP p50/p95/p99. Timing buffers cap at
20,000 and clear each window; memory retains only its current sample, with separate first/warmup/total
peak counters. The previous short-run harness had growing resource/client lists;
long measurements must not manufacture an unbounded measurement-memory trend.
Processes share the host: this is deployment evidence, not isolated hardware SLOs.

Targets: no corruption, no normal writer drops, healthy-node polling progress,
clean bounded shutdown; synthetic API window p95 <100ms, History p95 <250ms,
core interval p95 <=7.5s, small-payload RSS peak <128 MiB. Compare RSS and latency
across post-warmup windows, especially first/last 15-minute blocks. A sustained
RSS rise above 1 MiB/hour after the first 30 minutes, or >2x post-warmup HTTP p95
increase, triggers profiling/analysis; it is not automatically a proven leak.
Real-node freshness can reflect actual Agent/network conditions and requires
cause inspection rather than suppressing failures.

## Investigation and reassessment

Inspect live progress without restarting the measured Hub. Use a separate bounded
tracemalloc run (up to 30 minutes) if allocations need attribution; tracing changes
CPU/latency materially and is not compared as an uninstrumented performance result.
If a defect is isolated, make the smallest compatible fix, add a focused regression
check, run repository validation and repeat a representative long run under the
same settings. Extend/reassess only for unresolved evidence within the 09:00 limit.
Stop the affected experiment for corruption, lost healthy-node progress, memory
budget breach or stalled shutdown. Never fault-inject production Agents, restart
them, reboot hosts or deploy services as part of this read-only observation.

The result is bounded multi-hour evidence. It does not establish weeks of operation,
full seven-day/max-payload capacity, installed-service reboot acceptance or a formal
leak-free guarantee. Record tested durations, workload, faults and remaining limits.

Initial diagnostic runs were stopped after isolating a measurement bug: Windows
RSS sampling recreated ctypes pointer types each call, retaining them in the global
ctypes cache. A Hub-free 1,000-read reproduction grew the cache by 1,001 entries
and RSS by 11.543 MiB; the cached-reader fix held one structure/pointer type and
showed zero cache/RSS growth in the same probe. All acceptance runs restart with
this fix. Diagnostic profiles and aborted runs are not reported as completed soaks.

At about 50 minutes, real Linux RSS rose from 30.812 MiB at 30 minutes to
33.332 MiB, while the synthetic process remained near 40 MiB. Reassess with an
additional bounded 20-minute Linux field allocation probe, same 3-node GET-only
load plus the same Dashboard view pattern. The target is to attribute retained
Python objects; tracing timings are excluded from baseline comparisons. This
diagnostic completes before the original 90-minute acceptance run and remains
inside the user deadline. No runtime change is justified without attribution.
