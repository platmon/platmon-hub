#requires -Version 7.0
<#
Read-only Windows performance soak for an existing platmon-hub Python process.
CPU percentages are relative to one logical core; WorkingSet64 is resident working set.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateRange(1,2147483647)][int]$HubPid,
  [ValidateRange(1,86400)][int]$DurationSeconds = 1800,
  [ValidateRange(1,3600)][int]$SampleIntervalSeconds = 2,
  [ValidateRange(0,256)][int]$ExpectedNodeCount = 2,
  [ValidatePattern('^http://(127\.0\.0\.1|localhost):[0-9]{2,5}$')][string]$HubUrl = "http://127.0.0.1:9787",
  [string]$OutputDirectory = "soak-results",
  [switch]$RequireLive
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$null = Get-Process -Id $HubPid -ErrorAction Stop
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$csvPath = Join-Path $OutputDirectory "hub-soak-$stamp.csv"
$jsonPath = Join-Path $OutputDirectory "hub-soak-$stamp-summary.json"
$rows = [System.Collections.Generic.List[object]]::new()
$apiFailures = 0
$nodeCountFailures = 0
$liveFailures = 0
$lastCpu = $null
$lastTick = $null
$firstCpuSec = $null
$firstCpuTick = $null
$finalCpuSec = $null
$finalCpuTick = $null
$firstRssMiB = $null
$finalRssMiB = $null
$clock = [Diagnostics.Stopwatch]::StartNew()
$startUtc = [DateTime]::UtcNow

function Percentile95([object[]]$Source) {
  $nums = @($Source | Where-Object { $null -ne $_ -and "$_" -ne "" } |
    ForEach-Object { [double]$_ } | Where-Object { [double]::IsFinite($_) } | Sort-Object)
  if ($nums.Count -eq 0) { return $null }
  $idx = [Math]::Max(0, [int][Math]::Ceiling($nums.Count * 0.95) - 1)
  return [Math]::Round($nums[$idx], 3)
}

try {
  while ($clock.Elapsed.TotalSeconds -lt $DurationSeconds) {
    $tick = $clock.Elapsed.TotalSeconds
    $utc = [DateTime]::UtcNow.ToString("o")
    $proc = Get-Process -Id $HubPid -ErrorAction Stop
    $cpu = $proc.TotalProcessorTime.TotalSeconds
    $rss = [Math]::Round($proc.WorkingSet64 / 1MB, 3)
    if ($null -eq $firstCpuSec) {
      $firstCpuSec = $cpu
      $firstCpuTick = $tick
      $firstRssMiB = $rss
    }
    $finalCpuSec = $cpu
    $finalCpuTick = $tick
    $finalRssMiB = $rss
    $cpuPercent = $null
    if ($null -ne $lastCpu -and $null -ne $lastTick -and $tick -gt $lastTick) {
      $cpuPercent = [Math]::Round([Math]::Max(0, $cpu - $lastCpu) * 100 / ($tick - $lastTick), 3)
    }
    $lastCpu = $cpu
    $lastTick = $tick
    $request = [Diagnostics.Stopwatch]::StartNew()
    $errorCategory = $null
    $nodes = @()
    try {
      $body = Invoke-RestMethod -Uri "$HubUrl/api/nodes" -TimeoutSec 5
      if ($body.schema_version -ne 1 -or $null -eq $body.nodes) {
        throw "Invalid Hub schema"
      }
      $nodes = @($body.nodes | Where-Object { $null -ne $_ })
      if ($nodes.Count -ne $ExpectedNodeCount) { $nodeCountFailures++ }
    } catch {
      $apiFailures++
      $errorCategory = "hub_api_failure"
    } finally {
      $request.Stop()
    }
    $latency = [Math]::Round($request.Elapsed.TotalMilliseconds, 3)
    if ($nodes.Count -eq 0) {
      $nodes = @($null)
    }
    foreach ($node in $nodes) {
      if ($null -ne $node -and $RequireLive -and $node.enabled -and
          ($node.headline -ne "live" -or $node.data_current -ne $true -or $node.core_error)) {
        $liveFailures++
      }
      $rows.Add([PSCustomObject]@{
        Utc = $utc
        HubApiDurationMs = $latency
        HubApiError = $errorCategory
        HubCpuPercentOneCore = $cpuPercent
        HubRssMiB = $rss
        NodeId = if ($null -ne $node) { $node.node_id } else { $null }
        Name = if ($null -ne $node) { $node.name } else { $null }
        Headline = if ($null -ne $node) { $node.headline } else { $null }
        Current = if ($null -ne $node) { $node.data_current } else { $null }
        InstanceId = if ($null -ne $node) { $node.instance_id } else { $null }
        Sequence = if ($null -ne $node) { $node.sample_sequence } else { $null }
        DataAgeMs = if ($null -ne $node) { $node.data_age_ms } else { $null }
        CoreError = if ($null -ne $node) { $node.core_error } else { $null }
        OptionalError = if ($null -ne $node) { $node.optional_error } else { $null }
        LastCoreCompletedUtc = if ($null -ne $node) { $node.last_core_completed_utc } else { $null }
        LastCoreDurationMs = if ($null -ne $node) { $node.last_core_duration_ms } else { $null }
        LastOptionalCompletedUtc = if ($null -ne $node) { $node.last_optional_completed_utc } else { $null }
        LastOptionalDurationMs = if ($null -ne $node) { $node.last_optional_duration_ms } else { $null }
      })
    }
    $untilNext = [Math]::Min([double]$DurationSeconds, $tick + $SampleIntervalSeconds)
    $waitSeconds = $untilNext - $clock.Elapsed.TotalSeconds
    if ($waitSeconds -gt 0) {
      Start-Sleep -Milliseconds ([int][Math]::Ceiling($waitSeconds * 1000))
    }
  }
} finally {
  $clock.Stop()
  if ($rows.Count -gt 0) {
    $rows | Export-Csv -Path $csvPath -NoTypeInformation -Encoding utf8
  }
}
$nodesReport = @()
$nodeRows = @($rows | Where-Object { $null -ne $_.NodeId })
foreach ($group in ($nodeRows | Group-Object NodeId)) {
  $records = @($group.Group)
  $core = @($records | Where-Object { $_.LastCoreCompletedUtc -and $null -ne $_.LastCoreDurationMs } |
    Group-Object LastCoreCompletedUtc | ForEach-Object { $_.Group[0] })
  $optional = @($records | Where-Object { $_.LastOptionalCompletedUtc -and $null -ne $_.LastOptionalDurationMs } |
    Group-Object LastOptionalCompletedUtc | ForEach-Object { $_.Group[0] })
  $nodesReport += [PSCustomObject]@{
    node_id = $group.Name
    name = $records[0].Name
    samples = $records.Count
    live_count = @($records | Where-Object { $_.Headline -eq "live" }).Count
    not_current_count = @($records | Where-Object { $_.Current -ne $true }).Count
    core_error_count = @($records | Where-Object { $_.CoreError }).Count
    unique_core_polls_observed = $core.Count
    core_poll_p95_ms = Percentile95 @($core | ForEach-Object { $_.LastCoreDurationMs })
    optional_poll_p95_ms = Percentile95 @($optional | ForEach-Object { $_.LastOptionalDurationMs })
    max_data_age_ms = ($records | Measure-Object -Property DataAgeMs -Maximum).Maximum
  }
}
$cpuMeanPercent = $null
$cpuDeltaSec = $null
$cpuMeasurementSeconds = $null
if ($null -ne $firstCpuSec -and $null -ne $finalCpuSec -and
    $null -ne $firstCpuTick -and $null -ne $finalCpuTick) {
  $cpuMeasurementSeconds = $finalCpuTick - $firstCpuTick
  if ($cpuMeasurementSeconds -gt 0) {
    $cpuDeltaSec = [Math]::Max(0, $finalCpuSec - $firstCpuSec)
    $cpuMeanPercent = [Math]::Round(100 * $cpuDeltaSec / $cpuMeasurementSeconds, 4)
  }
}
$success = $apiFailures -eq 0 -and $nodeCountFailures -eq 0 -and
  (-not $RequireLive -or $liveFailures -eq 0) -and
  $clock.Elapsed.TotalSeconds -ge ($DurationSeconds - 0.2)
$summary = [ordered]@{
  schema_version = 1
  started_utc = $startUtc.ToString("o")
  ended_utc = [DateTime]::UtcNow.ToString("o")
  elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 3)
  duration_seconds = $DurationSeconds
  sample_interval_seconds = $SampleIntervalSeconds
  expected_nodes = $ExpectedNodeCount
  api_failure_count = $apiFailures
  node_count_mismatch_count = $nodeCountFailures
  required_live_violation_count = $liveFailures
  hub_api_p95_ms = Percentile95 @($rows | Where-Object { -not $_.HubApiError } | ForEach-Object { $_.HubApiDurationMs })
  hub_cpu_p95_percent_one_core = Percentile95 @($rows | ForEach-Object { $_.HubCpuPercentOneCore })
  hub_cpu_mean_percent_one_core = $cpuMeanPercent
  hub_cpu_total_seconds_between_samples = $cpuDeltaSec
  hub_cpu_elapsed_seconds_between_samples = $cpuMeasurementSeconds
  hub_rss_first_mib = $firstRssMiB
  hub_rss_last_mib = $finalRssMiB
  hub_rss_delta_mib = if ($null -ne $firstRssMiB -and $null -ne $finalRssMiB) {
    [Math]::Round($finalRssMiB - $firstRssMiB, 3)
  } else { $null }
  hub_rss_peak_mib = ($rows | Measure-Object -Property HubRssMiB -Maximum).Maximum
  nodes = @($nodesReport)
  passed = $success
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path $jsonPath -Encoding utf8
Write-Host "CSV: $csvPath"
Write-Host "Summary: $jsonPath"
Write-Host "Passed: $success"
Write-Host "CPU mean (% of one core): $cpuMeanPercent; CPU p95: $($summary.hub_cpu_p95_percent_one_core)"
Write-Host "RSS first/last/peak (MiB): $firstRssMiB / $finalRssMiB / $($summary.hub_rss_peak_mib)"
$nodesReport | Format-Table -AutoSize
if (-not $success) { exit 1 }
