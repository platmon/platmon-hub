# Foreground launcher for a source checkout or an installed Hub package.
[CmdletBinding()]
param(
    [string]$Registry,
    [string]$Python,
    [string]$StateDirectory,
    [ValidateRange(1,65535)][int]$Port = 9787,
    [switch]$ReadOnly,
    [string]$SettingsPath = (Join-Path $env:LOCALAPPDATA 'platmon-hub\launcher.json'),
    [switch]$SaveSettings,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$codeDirectory = Split-Path $PSScriptRoot -Parent
$defaults = @{
    Registry = (Join-Path $codeDirectory 'nodes.json')
    Python = 'python'
    StateDirectory = (Join-Path $env:LOCALAPPDATA 'platmon-hub')
    Port = 9787
    ReadOnly = $false
}
if (Test-Path -LiteralPath $SettingsPath) {
    if ((Get-Item -LiteralPath $SettingsPath).Length -gt 16384) { throw 'Launcher settings exceed 16 KiB.' }
    $saved = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $saved -or $saved -is [Array]) { throw 'Invalid launcher settings.' }
    foreach ($property in $saved.PSObject.Properties) {
        if (-not $defaults.ContainsKey($property.Name)) { throw 'Unknown launcher setting.' }
        if ($property.Name -eq 'ReadOnly') {
            if ($property.Value -isnot [bool]) { throw 'ReadOnly must be a boolean.' }
        } elseif ($property.Name -eq 'Port') {
            if (($property.Value -isnot [int] -and $property.Value -isnot [long]) -or $property.Value -lt 1 -or $property.Value -gt 65535) { throw 'Invalid launcher port.' }
        } elseif ($property.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($property.Value)) {
            throw 'Launcher paths must be nonempty strings.'
        }
        $defaults[$property.Name] = $property.Value
    }
}
foreach ($name in @('Registry', 'Python', 'StateDirectory', 'Port', 'ReadOnly')) {
    if ($PSBoundParameters.ContainsKey($name)) { $defaults[$name] = Get-Variable -Name $name -ValueOnly }
}
$registryPath = (Resolve-Path -LiteralPath $defaults.Registry).ProviderPath
if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) { throw 'Registry must be an existing file.' }
$pythonPath = @(Get-Command $defaults.Python -CommandType Application -ErrorAction Stop)[0].Source
$statePath = [IO.Path]::GetFullPath($defaults.StateDirectory)
$settingsFile = [IO.Path]::GetFullPath($SettingsPath)
foreach ($value in @($registryPath, $pythonPath, $statePath, $settingsFile)) {
    if ($value.Contains('"') -or $value.Contains("`r") -or $value.Contains("`n")) { throw 'Paths must not contain quotes or newlines.' }
}
$settings = [ordered]@{Registry=$registryPath; Python=$pythonPath; StateDirectory=$statePath; Port=$defaults.Port; ReadOnly=[bool]$defaults.ReadOnly}
if ($SaveSettings) {
    $parent = Split-Path $settingsFile -Parent
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary = Join-Path $parent ([IO.Path]::GetRandomFileName())
    try {
        [IO.File]::WriteAllText($temporary, ($settings | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $settingsFile) {
            [IO.File]::Replace($temporary, $settingsFile, ($settingsFile + '.bak'))
        } else {
            [IO.File]::Move($temporary, $settingsFile)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
    Write-Host 'Launcher settings saved. Future launches need no path arguments.'
}
if ($Check) {
    $settings | ConvertTo-Json
    return
}
[IO.Directory]::CreateDirectory($statePath) | Out-Null
$arguments = @('-m', 'hub', '--nodes', $registryPath, '--history-db', (Join-Path $statePath 'history.db'), '--log-file', (Join-Path $statePath 'hub.log'), '--port', [string]$defaults.Port)
if (-not $defaults.ReadOnly) { $arguments += '--web-admin' }
# Source checkouts need a predictable import directory; installed packages do not.
$workingDirectory = if (Test-Path -LiteralPath (Join-Path $codeDirectory 'hub\__main__.py')) { $codeDirectory } else { $statePath }
Write-Host ('Starting Hub at http://127.0.0.1:{0}/. Press Ctrl+C to stop.' -f $defaults.Port)
Push-Location -LiteralPath $workingDirectory
try {
    & $pythonPath @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Hub exited with an error. Check the console and the state directory log.' }
} finally { Pop-Location }
