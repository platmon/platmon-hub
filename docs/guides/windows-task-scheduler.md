# Windows startup before login with Task Scheduler

[Documentation index](../README.md) · [User guide](README.md) · [Operations](operations.md)

This is a separate, administrator-managed deployment for running Hub at machine
startup before anyone logs in. For automatic startup on your own Windows login,
use [Windows login startup](windows-startup.md) instead.

`install-windows-startup.ps1` creates a Startup-folder shortcut. It does **not**
create the task used here. If you already registered that shortcut, start it using
[the shortcut command](windows-startup.md#3-start-hub-now); do not continue with
Task Scheduler commands unless you are deliberately switching deployment methods.

## 1. Prepare the machine-wide installation

Install Python and the Hub wheel in a machine-accessible venv, for example
`$env:ProgramFiles\platmon-hub\venv`. An interactive user's Python installation
or WindowsApps alias is unsuitable for the Local Service account.
Prepare an administrator-owned registry under
`$env:ProgramData\platmon-hub\config` and a state directory under
`$env:ProgramData\platmon-hub\state`, preserving the existing registry's UUIDs.

Grant Local Service read/execute access to Python/venv, read-only access to the
registry, and modify access only to state. Retain administrative/SYSTEM access;
ordinary users must not be able to modify code, config, or state. Review NTFS
permissions before registration. These paths must already exist.

If switching from login startup, stop Hub and
[remove that Startup shortcut](windows-startup.md#optional-changes-and-removal)
before registering this deployment. Do not register both mechanisms for the same Hub.

## 2. Register the task

From **elevated PowerShell** in the repository root, set these variables to your
prepared paths. The defaults below derive their base directories from Windows:

```powershell
$taskOptions = @{
    Python = Join-Path $env:ProgramFiles 'platmon-hub\venv\Scripts\python.exe'
    Registry = Join-Path $env:ProgramData 'platmon-hub\config\nodes.json'
    StateDirectory = Join-Path $env:ProgramData 'platmon-hub\state'
}
.\deploy\install-windows-task.ps1 @taskOptions -WhatIf
```

After the preview succeeds and the account permissions are ready, register it:

```powershell
.\deploy\install-windows-task.ps1 @taskOptions
```

This script registers a task named `platmon-hub`. It refuses to overwrite an
existing task implicitly. A wheel installation or `-WhatIf` does not register it.

## 3. Start the registered task

Run these commands **only after step 2 successfully registers the task**:

```powershell
Start-ScheduledTask -TaskName platmon-hub
Get-ScheduledTaskInfo -TaskName platmon-hub
```

If either command reports that the file/task cannot be found, registration has
not succeeded for that task name. A Startup-folder shortcut is not a scheduled
task; follow its own [start command](windows-startup.md#3-start-hub-now).

## Operation and validation

The task uses Local Service and an AtStartup trigger, without interactive login
or a stored user password. It enables History and remains read-only by default.
See [Microsoft's service-account task principal example](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal).

`Stop-ScheduledTask -TaskName platmon-hub` can forcibly terminate the process and
is not a guaranteed graceful flush. To restart a scheduled instance, stop it,
verify process exit, then start it. Do not run a foreground instance against the
same database. Foreground Ctrl+C supports graceful drain.

Confirm reboot startup and the account's filesystem/network access on the actual
Windows host. CI checks script wiring and packaging; it does not install a task,
test reboot startup, or establish machine-specific permissions. See the
[operations validation boundary](operations.md#validation-boundary).
