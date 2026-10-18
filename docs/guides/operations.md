# Install and operate platmon-hub

[Documentation index](../README.md) · [User guide](README.md)

Hub runs on Python 3.9+ using only its standard library. Build tooling uses
setuptools/wheel; neither is a service dependency. A wheel is preferable to a
custom service wrapper: it preserves the existing Python entrypoint and includes
all allowlisted web assets. Deployment examples, empty registry/rule examples and
operational docs are installed under the environment’s `share/platmon-hub`. Use a dedicated virtual environment to avoid Python
package-name collisions. From 1.1.1 releases are published on [PyPI](https://pypi.org/project/platmon-hub/);
the same wheel is attached to the [GitHub releases](https://github.com/platmon/platmon-hub/releases) with its SHA-256 checksum.

## Install from PyPI

For a personal install, `pipx install platmon-hub` creates its own venv and puts the
`platmon-hub` command on your path; `pipx upgrade platmon-hub` updates it. pipx venvs
live in your user directory, so do not point a systemd unit or a Local Service task at one.

For a dedicated venv, as used by services and the Windows launcher, install a fixed version
with that venv's Python. These user-directory paths match the build example below:

**Windows PowerShell:**

```powershell
$venvDir = Join-Path $env:LOCALAPPDATA 'platmon-hub\venv'
python -m venv "$venvDir"
& (Join-Path $venvDir 'Scripts\python.exe') -m pip install --no-deps 'platmon-hub==1.1.1'
& (Join-Path $venvDir 'Scripts\platmon-hub.exe') --version
```

**Linux:**

```sh
venv_dir="$HOME/.local/share/platmon-hub/venv"
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --no-deps 'platmon-hub==1.1.1'
"$venv_dir/bin/platmon-hub" --version
```

Use the machine-wide venv paths of the systemd and Task Scheduler sections for those
services; the [Linux user service](#linux-user-service) uses this user venv. To upgrade a Hub that keeps history, follow
[backup, upgrade and rollback](#backup-upgrade-and-rollback): installing the new version
is one step of that procedure, and a database is never downgraded with the package.

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

## Linux user service

On a personal Linux computer, Hub can run as a systemd **user** service under your own
account, without a dedicated system user or administrator-owned files.
`deploy/platmon-hub-user.service` expects:

- The venv from [Install from PyPI](#install-from-pypi) at `~/.local/share/platmon-hub/venv`
  (a dedicated venv, not a pipx one).
- The registry at `~/.config/platmon-hub/nodes.json` and optional
  `~/.config/platmon-hub/hub.env` (same format as `deploy/hub.env.example`).
- The DB and log in `~/.local/state/platmon-hub/`, which must already exist.

```sh
(
set -e
venv_dir="$HOME/.local/share/platmon-hub/venv"
mkdir -p ~/.config/platmon-hub ~/.local/state/platmon-hub ~/.config/systemd/user
[ -e ~/.config/platmon-hub/nodes.json ] || echo '{"nodes": []}' > ~/.config/platmon-hub/nodes.json
cp "$venv_dir/share/platmon-hub/deploy/platmon-hub-user.service" ~/.config/systemd/user/platmon-hub.service
systemctl --user daemon-reload
systemctl --user enable --now platmon-hub
)
```

A user service starts when you log in and stops when your last session ends. To start it at
boot and keep it running after logout, enable lingering once (it may ask for your password):
`loginctl enable-linger "$USER"`. Operate it with `systemctl --user status|restart|stop platmon-hub`;
`journalctl --user -u platmon-hub` shows start-up errors and `~/.local/state/platmon-hub/hub.log`
the Hub log. To add options, such as raw capture, edit `ExecStart` with
`systemctl --user edit --full platmon-hub`.

Compared with the system unit, the registry and DB belong to your account, and the unit uses
less sandboxing (`UMask=0077` and `NoNewPrivileges` only). Use it only for a Hub you
operate yourself. Upgrade and roll back as in [Backup, upgrade and rollback](#backup-upgrade-and-rollback),
stopping with `systemctl --user stop platmon-hub`.

## Windows quick start and login startup

For a Windows PC where Hub should start when you log in, follow the dedicated
[Windows login startup guide](windows-startup.md). It covers saving one existing
registry path, registering the Startup shortcut, and starting that shortcut now.
If registration already printed `Created ... Hub was not started.`, go straight
to [Start Hub now](windows-startup.md#3-start-hub-now).

## Windows Task Scheduler example

Startup before login uses a separate administrator-managed installation. Follow
the [Task Scheduler guide](windows-task-scheduler.md) only for that deployment.
A login Startup shortcut does not create a scheduled task; the two guides are
alternatives, not consecutive setup steps.

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
stop the service and verify all Hub processes exited; install the new version in
the service's venv with that venv's Python (`python -m pip install --no-deps 'platmon-hub==NEW'`,
or the new wheel file); start and check live/history/reload diagnostics and unchanged node UUIDs.
Never store operational data inside the installed package. Schema migrations run
before history ingestion, create their own pre-migration backup of an existing
database (a newly created one gets none), and fail closed
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
operational rollout. See [release evidence](../development/release.md).
Release tagging and operational deployment require separate approval.
