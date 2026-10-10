# Run from elevated PowerShell after installing the wheel in a machine-wide venv.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Registry,
    [Parameter(Mandatory = $true)][string]$StateDirectory,
    [ValidateRange(1,65535)][int]$Port = 9787,
    [string]$TaskName = "platmon-hub"
)
$ErrorActionPreference = "Stop"
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$registryPath = (Resolve-Path -LiteralPath $Registry).Path
$statePath = (Resolve-Path -LiteralPath $StateDirectory).Path
foreach ($value in @($pythonPath, $registryPath, $statePath)) {
    if ($value.Contains('"') -or $value.Contains("`r") -or $value.Contains("`n")) {
        throw "Paths must not contain quotes or newlines."
    }
}
$dbPath = Join-Path $statePath "history.db"
$logPath = Join-Path $statePath "hub.log"
$arguments = '-m hub --nodes "{0}" --history-db "{1}" --log-file "{2}" --port {3}' -f $registryPath, $dbPath, $logPath, $Port
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $statePath
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\LOCAL SERVICE" -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
if ($PSCmdlet.ShouldProcess($TaskName, "Register startup task running as Local Service")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
}
