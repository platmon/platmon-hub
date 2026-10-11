# Install and operate platmon-hub

[Documentation index](../README.md) · [User guide](README.md)

Hub runs on Python 3.9+ using only its standard library. Build tooling uses
setuptools/wheel; neither is a service dependency. A wheel is preferable to a
custom service wrapper: it preserves the existing Python entrypoint and includes
all allowlisted web assets. Deployment examples, empty registry/rule examples and
operational docs are installed under the environment’s `share/platmon-hub`. Use a dedicated virtual environment to avoid Python
package-name collisions. The current `0.2.0rc1` candidate is not a release tag.

## Build and local install

Run from the **repository root** containing `pyproject.toml`. These examples use
your user directory for the venv and a fresh temporary directory for each wheel
build. The wheel filename is selected automatically, so no path or version
placeholder needs replacing. Python needs `pip` and `venv` support; build tooling
may be downloaded from the package index.

**Windows PowerShell** (use `py -3` in place of `python` if needed):

```powershell
$wheelDir = Join-Path $env:TEMP ('platmon-hub-wheels-' + [guid]::NewGuid())
$venvDir = Join-Path $env:LOCALAPPDATA 'platmon-hub\venv'
python -m pip wheel --no-deps --wheel-dir "$wheelDir" .
if ($LASTEXITCODE -ne 0) { throw 'Wheel build failed' }
python -m venv "$venvDir"
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
$wheel = @(Get-ChildItem -LiteralPath $wheelDir -Filter 'platmon_hub-*.whl')
if ($wheel.Count -ne 1) { throw 'Expected exactly one Hub wheel' }
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
& $venvPython -m pip install --no-deps $wheel[0].FullName
if ($LASTEXITCODE -ne 0) { throw 'Wheel installation failed' }
& (Join-Path $venvDir 'Scripts\platmon-hub.exe') --version
```

**Linux** (the parentheses keep variables and `set -e` local to this block):

```sh
(
set -e
wheel_dir="$(mktemp -d -t platmon-hub-wheels.XXXXXX)"
venv_dir="$HOME/.local/share/platmon-hub/venv"
python3 -m pip wheel --no-deps --wheel-dir "$wheel_dir" .
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --no-deps "$wheel_dir"/platmon_hub-*.whl
"$venv_dir/bin/platmon-hub" --version
)
```

Change only `$venvDir` / `venv_dir` if you want another installation location.
These defaults are for the current user. For systemd or the Local Service task
below, use the machine-wide paths and account permissions in that section.
The installed docs and deployment scripts are under the venv's
`share/platmon-hub`; installing a wheel does not start Hub or register startup.

Keep code/venv, registry, logs and database in separate directories. Start with an
empty registry (`{"nodes": []}`), then add only approved endpoints using the
[local CLI](setup.md#local-registry-cli). UUIDs live in the registry, not in
code or the venv. Default binding remains `127.0.0.1:9787`; choose another fixed
port explicitly with `--port`, never 0 or an automatic fallback.

## Linux systemd example

The shipped `deploy/platmon-hub.service` expects:

- A dedicated `platmon-hub` system user/group with no login shell.
- The installed venv at `/opt/platmon-hub/venv` (service user can read/execute).
- An administrator-owned registry `/etc/platmon-hub/nodes.json`, readable by the
  service group; the service cannot modify this directory.
- Optional `/etc/platmon-hub/hub.env` copied from `deploy/hub.env.example`.

After reviewing paths, create the account/config directory and install the unit
as an administrator. `systemctl daemon-reload` and
`systemctl enable --now platmon-hub` enable automatic startup. Stop/restart with
`systemctl stop platmon-hub` / `systemctl restart platmon-hub`.
`systemctl status platmon-hub` and `journalctl -u platmon-hub` aid diagnosis.
The unit creates `/var/lib/platmon-hub` and `/var/log/platmon-hub` for DB and logs.
It uses filesystem restrictions and a 30-second service stop limit. SIGTERM
requests normal Hub shutdown and writer drain; a stuck OS DNS/filesystem call
may exceed this and force termination, losing queued but uncommitted history.
See [systemd's directory/permission model](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).

## Windows quick start and login startup

For a source checkout, stop any existing Hub using this Registry/DB before changing
preview versions or starting the launcher. Open PowerShell in the **repository
root** containing `start-hub.cmd`.

Choose the location of your **existing** private registry, preserving its UUIDs.
If `nodes.json` is in this checkout next to `start-hub.cmd`, resolve and save it:

```powershell
.\start-hub.cmd -Registry .\nodes.json -SaveSettings -Check
```

If you created the registry using the [setup guide](setup.md#register-agents-and-start-monitoring),
use its user-specific path **instead**:

```powershell
.\start-hub.cmd -Registry "$env:LOCALAPPDATA\platmon-hub\nodes.json" -SaveSettings -Check
```

Both commands check paths and save settings without starting Hub. If your existing
registry is elsewhere, supply that file's actual path with `-Registry`; the
launcher does not move or create it. If you have no registry yet, complete the
[setup guide](setup.md#register-agents-and-start-monitoring) first. Do not replace
an existing registry with the empty example.

After the chosen command succeeds, start Hub. Later launches use the same command:

```powershell
.\start-hub.cmd
```

The launcher uses the Python resolved on the first setup, loopback port 9787,
`--web-admin`, and `%LOCALAPPDATA%\platmon-hub\history.db` / `hub.log`.
It stores only local paths, port and read-only preference in that directory's
`launcher.json`; it never copies/deletes the Registry or stores a token. Keep
this file private and outside Git. An explicit `-Registry` overrides the saved path;
without either, a source checkout defaults to its own `nodes.json`.
Use `-Python` with an existing executable path, `-StateDirectory PATH`, `-Port PORT`,
or `-ReadOnly` to override a saved value;
`-SaveSettings` persists overrides with a `.bak` copy. `-Check` validates/resolves
settings without starting Hub; combine it with `-SaveSettings` to configure only.
A missing Registry is an error; the launcher does not discover or register Agents.
Ctrl+C requests graceful shutdown. Logs rotate using the existing Hub settings.

If PowerShell blocks a downloaded script, review it first, then use
`Unblock-File` on the two scripts in `deploy`; no execution-policy bypass is used.
An enforced organizational policy needs the organization's supported signing or
execution route. For a wheel install, invoke the installed
`share/platmon-hub/deploy/start-windows.ps1` directly instead of `start-hub.cmd`.

Register automatic start **at current-user login**, without administrator rights:

```powershell
.\deploy\install-windows-startup.ps1 -WhatIf
.\deploy\install-windows-startup.ps1
# Optional manual-launch shortcut on your Desktop:
.\deploy\install-windows-startup.ps1 -Desktop
# Remove automatic start; this does not stop an already running Hub:
.\deploy\install-windows-startup.ps1 -Remove
# Remove the optional Desktop shortcut:
.\deploy\install-windows-startup.ps1 -Desktop -Remove
```

Setup creates `platmon-hub.lnk` in the user's Startup folder and points at the
reviewed launcher and settings paths. It does not start Hub immediately, overwrite
an existing shortcut, elevate privileges, store passwords or register a system
service. The launcher window starts minimized, remains available for Ctrl+C,
and uses the same log/History paths. To change settings, stop Hub and rerun the
launcher with overrides plus `-SaveSettings`; no shortcut recreation is needed.
If the checkout moves, remove/recreate its shortcut. Keep the preview checkout
until its shortcut is removed. Avoid starting another foreground instance while
the login instance is running: fixed-port binding rejects the duplicate.

Startup-folder launch requires login and is not a service/watchdog; it does not
restart after failure. Open `shell:startup` to inspect the shortcut. Use the
Task Scheduler deployment below when startup before login is required; it uses
a separate account/permission model and remains read-only by default. Do not
register both mechanisms for the same Hub. Login/reboot verification must be
performed on the target host; these scripts are not installed or registered by CI.

## Windows Task Scheduler example

Install Python/venv in a machine-accessible directory such as
`C:\Program Files\platmon-hub\venv`, not an interactive user's profile.
Prepare an administrator-owned config directory and
`C:\ProgramData\platmon-hub\state`. Grant Local Service read/execute access to
Python/venv, read-only access to the registry, and modify access only to state.
Retain administrative/SYSTEM access, and do not grant ordinary users write
access to code, config or state. Review NTFS ACLs before registering the task.

From elevated PowerShell, after substituting actual reviewed paths:

```powershell
./deploy/install-windows-task.ps1 -Python 'C:\Program Files\platmon-hub\venv\Scripts\python.exe' -Registry 'C:\ProgramData\platmon-hub\config\nodes.json' -StateDirectory 'C:\ProgramData\platmon-hub\state' -WhatIf
./deploy/install-windows-task.ps1 -Python 'C:\Program Files\platmon-hub\venv\Scripts\python.exe' -Registry 'C:\ProgramData\platmon-hub\config\nodes.json' -StateDirectory 'C:\ProgramData\platmon-hub\state'
Start-ScheduledTask -TaskName platmon-hub
Get-ScheduledTaskInfo -TaskName platmon-hub
```

The task uses Local Service and an AtStartup trigger, without interactive login
or a stored user password. It refuses to overwrite an existing task implicitly.
See [Microsoft's service-account task principal example](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal).
`Stop-ScheduledTask -TaskName platmon-hub` can forcibly terminate the process;
it is not a guaranteed graceful flush. Foreground Ctrl+C supports graceful drain.
To restart a scheduled instance, stop it, verify process exit, then start it.
Do not run a foreground instance against its same database. Confirm reboot
startup and the account's filesystem/network access on the actual Windows host.
No service or scheduled task is installed by the wheel or CI.

## Logging and health

`--log-file PATH` selects UTF-8 rotation at 2 MiB with three backups; otherwise
logs go to stderr. The parent directory must exist and be writable. Protect logs
as operational data. `/healthz` is process responsiveness, not fleet health.
Inspect `/api/history/status` and `/api/registry/status` for storage/reload issues.
History failure leaves live polling active; drop counts can mean incomplete data.

## Backup, upgrade and rollback

`platmon-hub backup --db /path/to/history.db --output /path/to/new-backup.db`
uses SQLite's online backup API and verifies the resulting database. It refuses
an existing destination. Keep a separately protected copy of the registry (and
alert settings when present). Do not copy an active DB file alone or delete its
WAL/SHM files. Test restoring a backup before relying on it.

For upgrade: preserve the previous wheel and config; take a verified backup;
stop the service and verify all Hub processes exited; install the new wheel in
the venv; start and check live/history/reload diagnostics and unchanged node UUIDs.
Never store operational data inside the installed package. Schema migrations run
before history ingestion, create their own pre-migration backup, and fail closed
for history without destroying old data. A newer unknown schema is rejected.

For rollback: stop Hub, preserve the failed DB and its sidecars together for
investigation, restore a compatible verified backup to a clean DB path, restore
the corresponding configuration, install the previous wheel, and start with that
path. Restoring an older backup loses later observations; obtain operator consent
for that loss. Code downgrade does not imply automatic database downgrade.

## Validation boundary

CI builds and installs the wheel on Ubuntu/Windows with Python 3.9/3.13, checks
packaged assets, parses the Task Scheduler script and validates a systemd unit
with a local executable substitution. These are not reboot or account-permission
tests. Short foreground collection and process restart with three real Agents passed on
Linux and native Windows. Actual Windows 11/systemd installation, reboot startup,
installed-service upgrades and machine-specific ACLs remain to be verified before
operational rollout. See [candidate evidence](../development/release.md).
Release tagging and operational deployment require separate approval.
