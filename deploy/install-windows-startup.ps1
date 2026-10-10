# Register a current-user login shortcut; does not start Hub or require elevation.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SettingsPath = (Join-Path $env:LOCALAPPDATA 'platmon-hub\launcher.json'),
    [switch]$Remove,
    [switch]$Desktop
)
$ErrorActionPreference = 'Stop'
$folder = if ($Desktop) { [Environment]::GetFolderPath('DesktopDirectory') } else { [Environment]::GetFolderPath('Startup') }
if ([string]::IsNullOrWhiteSpace($folder)) { throw 'The current-user shortcut directory is unavailable.' }
$shortcutPath = Join-Path $folder 'platmon-hub.lnk'
if ($Remove) {
    if ((Test-Path -LiteralPath $shortcutPath) -and $PSCmdlet.ShouldProcess($shortcutPath, 'Remove Hub shortcut')) {
        Remove-Item -LiteralPath $shortcutPath
    }
    return
}
if (Test-Path -LiteralPath $shortcutPath) { throw 'Hub shortcut already exists; remove it explicitly before replacing it.' }
$launcher = Join-Path $PSScriptRoot 'start-windows.ps1'
$settingsFile = (Resolve-Path -LiteralPath $SettingsPath).ProviderPath
foreach ($value in @($launcher, $settingsFile)) {
    if ($value.Contains('"') -or $value.Contains("`r") -or $value.Contains("`n")) { throw 'Paths must not contain quotes or newlines.' }
}
# Validate settings without starting Hub or writing state.
& $launcher -SettingsPath $settingsFile -Check | Out-Null
if ($PSCmdlet.ShouldProcess($shortcutPath, 'Create current-user Hub shortcut')) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $shortcut.Arguments = '-NoProfile -File "{0}" -SettingsPath "{1}"' -f $launcher, $settingsFile
    $shortcut.WorkingDirectory = Split-Path $PSScriptRoot -Parent
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'platmon Hub local fleet monitoring'
    $shortcut.Save()
    Write-Host ('Created {0}. Hub was not started.' -f $shortcutPath)
}
