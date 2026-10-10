# Install and operate platmon-hub

Hub runs on Python 3.9+ using only its standard library. Build tooling uses
setuptools/wheel; neither is a service dependency. A wheel is preferable to a
custom service wrapper: it preserves the existing Python entrypoint and includes
all allowlisted web assets. Use a dedicated virtual environment to avoid Python
package-name collisions. The current development version is not a release tag.

## Build and local install

```sh
python -m pip wheel --no-deps --wheel-dir /path/to/wheels .
python -m venv /path/to/venv
/path/to/venv/bin/python -m pip install --no-deps /path/to/wheels/platmon_hub-VERSION-py3-none-any.whl
/path/to/venv/bin/platmon-hub --version
```

On Windows use `venv\Scripts\python.exe` and `venv\Scripts\platmon-hub.exe`.
Keep code/venv, registry, logs and database in separate directories. Start with an
empty registry (`{"nodes": []}`), then add only approved endpoints using the
[local CLI](../backend.md#local-registry-cli). UUIDs live in the registry, not in
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
tests. Actual Windows 11/systemd installation, restart/reboot, upgrades with live
agents and machine-specific ACLs must be verified before operational rollout.
Release tagging and operational deployment require separate approval.
