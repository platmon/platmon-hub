# Non-operational checks: temporary settings only; no real startup registration.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
foreach ($name in @('install-windows-task.ps1', 'start-windows.ps1', 'install-windows-startup.ps1')) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root ('deploy\' + $name)), [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
}
$work = Join-Path ([IO.Path]::GetTempPath()) ('platmon launcher ' + [Guid]::NewGuid().ToString())
[IO.Directory]::CreateDirectory($work) | Out-Null
try {
    $registry = Join-Path $work 'nodes.json'
    [IO.File]::WriteAllText($registry, '{"nodes": []}')
    $settings = Join-Path $work 'launcher.json'
    $state = Join-Path $work 'state'
    $pythonPath = (Get-Command python -CommandType Application).Source
    $launcher = Join-Path $root 'deploy\start-windows.ps1'
    $startup = Join-Path $root 'deploy\install-windows-startup.ps1'
    & $launcher -Registry $registry -Python $pythonPath -StateDirectory $state -SettingsPath $settings -SaveSettings -Check | Out-Null
    $loaded = & $launcher -SettingsPath $settings -Check | ConvertFrom-Json
    if ($loaded.Registry -ne $registry -or $loaded.Python -ne $pythonPath -or $loaded.Port -ne 9787 -or $loaded.ReadOnly) {
        throw 'Saved launcher settings did not round-trip.'
    }
    $override = & $launcher -SettingsPath $settings -ReadOnly -Port 9798 -SaveSettings -Check | ConvertFrom-Json
    if (-not $override.ReadOnly -or $override.Port -ne 9798 -or -not (Test-Path ($settings + '.bak'))) {
        throw 'Explicit overrides or settings backup failed.'
    }
    $startupFolder = [Environment]::GetFolderPath('Startup')
    $shortcut = Join-Path $startupFolder 'platmon-hub.lnk'
    # Do not disturb a developer's existing shortcut. CI starts without one.
    if (-not (Test-Path -LiteralPath $shortcut)) { & $startup -SettingsPath $settings -WhatIf }
    if (Test-Path -LiteralPath $state) { throw 'Check/WhatIf unexpectedly created Hub state.' }
    # Empty-registry, loopback-only launch smoke; port 0 is used only to reserve a test port.
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    & $launcher -SettingsPath $settings -ReadOnly:$false -Port $port -SaveSettings -Check | Out-Null
    $powershell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $process = Start-Process -FilePath $powershell -ArgumentList ('-NoProfile -File "{0}" -SettingsPath "{1}"' -f $launcher, $settings) -PassThru -RedirectStandardOutput (Join-Path $work 'stdout.log') -RedirectStandardError (Join-Path $work 'stderr.log')
    try {
        $ready = $false
        for ($i = 0; $i -lt 50; $i++) {
            if ($process.HasExited) { throw 'Launcher exited before Hub became healthy.' }
            try {
                $health = Invoke-RestMethod ("http://127.0.0.1:$port/healthz") -TimeoutSec 1
                if ($health.status -eq 'ok') { $ready = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 100
        }
        if (-not $ready) { throw 'Launcher Hub readiness deadline exceeded.' }
        $admin = Invoke-RestMethod ("http://127.0.0.1:$port/api/admin/status") -TimeoutSec 1
        $history = Invoke-RestMethod ("http://127.0.0.1:$port/api/history/status") -TimeoutSec 1
        $nodes = Invoke-RestMethod ("http://127.0.0.1:$port/api/nodes") -TimeoutSec 1
        if (-not $admin.enabled -or -not $history.history.enabled -or $nodes.nodes.Count -ne 0) {
            throw 'Launcher did not apply saved Registry, History and web management settings.'
        }
    } finally {
        # Force termination is restricted to this temporary empty-registry test.
        Get-CimInstance Win32_Process -Filter ("ParentProcessId = {0}" -f $process.Id) | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
        $process.WaitForExit()
    }
    [IO.File]::WriteAllText($settings, '{"Port":0}')
    $rejected = $false
    try { & $launcher -SettingsPath $settings -Check | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid saved port was accepted.' }
    [IO.File]::WriteAllText($settings, '{"Unknown":true}')
    $rejected = $false
    try { & $launcher -SettingsPath $settings -Check | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Unknown saved setting was accepted.' }
    Write-Host 'Launcher syntax, settings, overrides, backup, validation, startup WhatIf and empty-registry Hub launch passed.'
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force
}
