# Windows CI and 30-minute fleet soak

Run this **read-only** test on the Windows PC already hosting Hub. It polls only Hub's local API, never scans or controls Orin/Pi agents. Runtime uses Python stdlib; the measurement harness uses PowerShell 7 and Windows Get-Process.

## CI matrix

The existing unit-test workflow now runs on `ubuntu-latest` and `windows-latest`, with Python 3.9 and 3.13 (four jobs). Every job runs `unittest`, `compileall` and JavaScript syntax validation. Windows jobs also launch a loopback Hub with an **empty registry** and run a 4-second soak harness smoke. CI does not access real equipment or measure 30-minute stability.

## Reproduce on Windows (Orin Nano + Raspberry Pi)

1. Check out this branch until merged, preserve your existing private `nodes.json`, and use the already-running device Agents. Do not modify their configurations.
2. In terminal A start Hub as usual: `python -m hub --nodes .\nodes.json`.
3. In another PowerShell 7 terminal, identify the **Hub Python process PID** (not the shell or another Python process):

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match ' -m hub ' } |
  Select-Object ProcessId, CommandLine
```

4. Check basic script wiring first, using the actual PID:

```powershell
./scripts/soak-windows.ps1 -HubPid 12345 -DurationSeconds 15 -ExpectedNodeCount 2
```

5. Run a 30-minute healthy workload. The script does not stop or restart Hub or Agents:

```powershell
./scripts/soak-windows.ps1 -HubPid 12345 -DurationSeconds 1800 -SampleIntervalSeconds 2 -ExpectedNodeCount 2 -RequireLive -OutputDirectory "$env:USERPROFILE\platmon-hub-soak"
```

`-RequireLive` marks a run failed when an enabled device is not `live`, `data_current` becomes false or a Core error is observed. This condition is suitable only for a normal healthy run; **do not enable** it for intentional failover testing. A successful run is limited to the tested equipment and environment, not a general fleet-size claim.

## Output and semantics

The script writes `hub-soak-<UTC>.csv` and `hub-soak-<UTC>-summary.json`. Keep these files private when they contain device names or process identities.

| Metric | Actual meaning |
| --- | --- |
| `HubApiDurationMs` | Windows browser-host to local Hub `/api/nodes` round-trip + JSON decoding time |
| `LastCoreDurationMs` | Hub-measured elapsed time for last `/api/status` and conditional `/api/stats` call chain (success or failure) |
| `LastOptionalDurationMs` | Hub-measured elapsed time for optional `/api/observations` request |
| `LastCoreCompletedUtc` | Completion timestamp used to deduplicate repeated reads of the same poll |
| `HubCpuPercentOneCore` | Hub process CPU-time delta / local elapsed-time delta × 100; may exceed 100 on multicore |
| `hub_cpu_mean_percent_one_core` | Run-level CPU time delta divided by the elapsed time **between first and last process samples**, as percent of one core; preferable to sparse 2-second-bucket CPU p95 for low loads |
| `hub_cpu_total_seconds_between_samples` | Cumulative Hub process CPU seconds accrued between first and last snapshots; not total lifetime process CPU |
| `hub_rss_first_mib`, `hub_rss_last_mib`, `hub_rss_delta_mib` | First/last process working set and difference; a positive delta alone does not establish a memory leak |
| `HubRssMiB` | Windows process working set in MiB, not allocated Python heap |
| `Headline`, `Current`, `CoreError`, `InstanceId`, `Sequence`, `DataAgeMs` | Hub's observed node status and last-known identity/freshness |

p95 Hub and per-Agent poll durations and peak Hub working set are derived from observed samples. Duplicate reads of a single Agent poll are counted only once using `LastCoreCompletedUtc`. **A Hub poll duration is not sensor acquisition time on the Agent.** The UTC completion timestamp is for correlation, not monotonic timing; system clock changes may affect uniqueness. The measurement itself uses Hub elapsed time.

Stop the sampler with Ctrl+C if necessary; partial CSV data is saved. A failed or interrupted run is not a completed 30-minute pass. Avoid committing measured private files to the public repository.

## Acceptance evidence and limits

Record Windows/PowerShell/Python versions, Hub git commit, Orin/Pi Agent versions, CPU p95 (% of one core), RSS peak, Hub API p95, Core poll p95 per node, observed stale/offline/errors, sample sequence changes and actual elapsed duration. Do not invent performance thresholds before a baseline exists.

The manual Windows + two-device test completed **1800.01 seconds**, reporting 896/896 live observations per node, zero Core errors, Hub API p95 3.092ms and peak working set 38.914 MiB (operator-generated summary supplied 2026-10-10). The earlier script reported CPU bucket p95 0, which is not evidence of zero CPU cost; the new cumulative CPU-mean and RSS start/end fields should be captured in a subsequent measurement, not retroactively imputed. CI's synthetic 4-second smoke cannot replace the real run. The Hub's Python urllib timeout also cannot forcibly abort every DNS resolution.

