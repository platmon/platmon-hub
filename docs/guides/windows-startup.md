# Windows login startup

[Documentation index](../README.md) · [User guide](README.md) · [Operations](operations.md)

Use this guide to run Hub on your Windows PC and start it automatically when you
log in. It uses a Startup-folder shortcut and needs no administrator rights.
The shortcut opens a minimized console. Registration and starting Hub are separate
steps.

**Already saw `Created ...\Startup\platmon-hub.lnk. Hub was not started.`?**
Registration is complete: go directly to [Start Hub now](#3-start-hub-now).
Task Scheduler commands belong to a [separate deployment](windows-task-scheduler.md)
for startup before login; they are not part of this procedure.

Run the setup commands in PowerShell from the repository root containing
`start-hub.cmd`. Stop an existing Hub before starting another instance with the
same registry/database.

## 1. Save one existing registry path

If `nodes.json` is next to `start-hub.cmd`, run this block once:

```powershell
$registryPath = '.\nodes.json'
.\start-hub.cmd -Registry "$registryPath" -SaveSettings -Check
if ($LASTEXITCODE -ne 0) { throw 'Launcher setup failed; correct the registry path before continuing.' }
```

If your registry is elsewhere, change **only the first line** before running the
block. For a registry created by the [setup guide](setup.md#register-agents-and-start-monitoring),
that line is `$registryPath = Join-Path $env:LOCALAPPDATA 'platmon-hub\nodes.json'`.
Choose the file that actually exists and contains your devices; these are
alternative locations, not two setup steps. If you have no registry yet, complete
the setup guide first. Preserve existing UUIDs and contents.

The output must show your chosen file in `Registry`. `-SaveSettings -Check` saves
its absolute path without starting Hub or copying the registry. A failed path
check leaves previously saved settings intact. In particular, saving
`.\nodes.json` does not create another `nodes.json` under `%LOCALAPPDATA%`.

## 2. Register login startup once

Skip this step if the shortcut was already created. Otherwise:

```powershell
.\deploy\install-windows-startup.ps1
```

`Created ...\Startup\platmon-hub.lnk. Hub was not started.` means registration
succeeded. The script creates a shortcut in the current user's Startup folder;
it does not register a scheduled task. It refuses to overwrite an existing shortcut.
Use `-WhatIf` only if you want a preview before registration.

## 3. Start Hub now

Start the registered shortcut once, or wait for your next Windows login:

```powershell
$shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'platmon-hub.lnk'
Invoke-Item -LiteralPath $shortcut
```

The minimized Hub window keeps running after you close the PowerShell window that
opened the shortcut. Open [http://127.0.0.1:9787/](http://127.0.0.1:9787/) after a
few seconds. With the default port, this command should report `status: ok`:

```powershell
Invoke-RestMethod 'http://127.0.0.1:9787/healthz'
```

Use your configured port if you changed it. To stop Hub, restore its minimized
window and press **Ctrl+C**. With a foreground `start-hub.cmd` session, answering
`Y` to the batch termination prompt also ends the session; Hub is then stopped.
The next Windows login starts Hub again while the Startup shortcut remains.

Do not start the shortcut twice or run another Hub on the same port/database.
Startup-folder launch requires login and does not restart Hub after a failure.
Test login startup on the target host; CI does not register this shortcut.

## Settings and manual foreground execution

Settings are saved in `%LOCALAPPDATA%\platmon-hub\launcher.json`. The launcher uses
the resolved Python executable, loopback port 9787, local web device management,
and `%LOCALAPPDATA%\platmon-hub\history.db` / `hub.log` by default. The registry
stays at the path you selected. Keep settings, the registry, and runtime data private.

For a manual foreground session, run `.\start-hub.cmd` and keep that terminal
open. This is an alternative to opening the shortcut in step 3.

To change settings, stop Hub and rerun the launcher with explicit overrides and
`-SaveSettings -Check`. `-Registry`, `-Python`, `-StateDirectory`, `-Port`, and
`-ReadOnly` override saved values; an explicit registry path takes precedence.
Without a saved or explicit registry, a source checkout defaults to its own
`nodes.json`. Saving over an existing settings file creates a `.bak` copy.
Inspect current settings without starting Hub with `.\start-hub.cmd -Check`.

If PowerShell blocks a downloaded script, review it and use `Unblock-File` on
`deploy/start-windows.ps1` and `deploy/install-windows-startup.ps1`. An enforced
organizational policy needs the supported signing/execution route.
For an [installed wheel](operations.md#build-and-local-install), use the scripts
under the venv's `share/platmon-hub/deploy` instead of the checkout's scripts;
invoke `start-windows.ps1` directly in place of `start-hub.cmd`.

## Optional changes and removal

These are maintenance actions, not additional setup steps. If the checkout moves,
remove its old shortcut and register the new location. Open `shell:startup` to
inspect the shortcut; keep its referenced checkout until you remove it.

**Only to disable automatic login startup** (this does not stop a running Hub):

```powershell
.\deploy\install-windows-startup.ps1 -Remove
```

**Only to add a manual Desktop shortcut:**

```powershell
.\deploy\install-windows-startup.ps1 -Desktop
```

**Only to remove the Desktop shortcut:**

```powershell
.\deploy\install-windows-startup.ps1 -Desktop -Remove
```
