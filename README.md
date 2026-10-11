# platmon-hub

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="hub/web/assets/brand/platmon-logo-dark.svg">
  <img src="hub/web/assets/brand/platmon-logo-light.svg" alt="platmon — approved C / Terminal P brand" width="300">
</picture>

Centralized, read-only Fleet monitoring for independent [platmon](https://github.com/platmon/platmon) agents, with a Windows/Linux Hub and Python 3.9+ standard-library runtime.

> **Status: 0.2.0rc1 candidate.** History, registry management, events, internal alerts and packaging are implemented. Three real devices passed short read-only collection/restart checks on Linux and native Windows. Synthetic 100-node and actual three-device Windows/Linux 90-minute runs passed collection and absolute resource budgets. Working-set limits and remaining growth are documented. Service reboot/ACL acceptance and a longer operational pilot remain before production rollout; no release tag or deployment is implied.

Agents own hardware collection and remain independent when the Hub stops. The Hub
uses explicit registry entries and fixed GET paths. It never imports Agent code,
scans networks, controls devices or uses Agent in-memory history as persistence.

## Start locally

```sh
python -m pip install --no-deps /path/to/platmon_hub-0.2.0rc1-py3-none-any.whl
platmon-hub nodes --file /private/nodes.json add --name "Approved device" --endpoint http://192.0.2.1:9797
platmon-hub --nodes /private/nodes.json --history-db /private/history.db
```

The IP above is documentation-only; substitute an explicitly approved Agent origin.
The parent directory must exist. The CLI assigns a stable UUID; endpoint/name
changes preserve it. Without installation, use `python -m hub` in place of
`platmon-hub`. See [build and installation](docs/operations.md) to create the wheel.
Open `http://127.0.0.1:9787/`. The example registry contains no active targets.

Use the **Devices** tab in the Dashboard after enabling [local web registration](docs/web-registration.md), or add an approved device from a local terminal:

```sh
python -m hub nodes --file nodes.json add --interactive
# Or supply the name and Agent IP; Agent port defaults to 9797:
python -m hub nodes --file nodes.json add --name "Lab node" --host 192.0.2.1
```

The running Hub automatically reloads its registry; no Agent or Hub restart is
needed by default. See [local registry CLI](backend.md#local-registry-cli) for
custom ports, disabled registration, validation and cancellation.

History is opt-in via `--history-db`; internal alerts additionally require
`--alerts /private/alerts.json`. Review [example rules](alerts.example.json) against
your devices. Defaults retain metrics for 7 days and events for 30 days. Storage
failure does not stop live polling, but queued history can be lost. A bounded
History API and local Canvas graphs preserve missing values and restart boundaries.

Keep Hub loopback-only and Agents on a trusted management network. The optional
[TLS/authentication proxy](docs/security.md) is a separate, explicitly configured
component. HTTP is read-only by default. Opt-in [local web device management](docs/web-registration.md) is enabled with `--web-admin` for trusted local users.

## Windows quick start

From PowerShell in the source checkout, stop any existing Hub using the same
Registry/DB before launching. Save your existing Registry path once:

```powershell
.\start-hub.cmd -Registry 'C:\platmon-config\nodes.json' -SaveSettings -Check
```

Then start with one command, or double-click `start-hub.cmd`:

```powershell
.\start-hub.cmd
```

The launcher remembers the Registry/Python paths in
`%LOCALAPPDATA%\platmon-hub\launcher.json`. It enables History and local web
management, using loopback `127.0.0.1:9787` and that directory's `history.db` /
`hub.log`. Existing Registry UUIDs and History are preserved. Use `-Python PATH`
for a particular interpreter, `-StateDirectory PATH` for an existing data
location, or `-ReadOnly` to disable web management; add `-SaveSettings` to remember
an override. Press Ctrl+C to stop the foreground instance.

To start automatically **at current-user login**, use another PowerShell window
in the checkout (no administrator rights needed):

```powershell
.\deploy\install-windows-startup.ps1 -WhatIf
.\deploy\install-windows-startup.ps1
```

This creates a minimized-launch shortcut in the user's Startup folder; it does
not start Hub immediately or overwrite an existing shortcut. Optional Desktop
shortcut and automatic-start removal are separate commands:

```powershell
# Optional manual-launch shortcut:
.\deploy\install-windows-startup.ps1 -Desktop
# Remove login startup without stopping the running Hub:
.\deploy\install-windows-startup.ps1 -Remove
```

Run only the command for the action you want. Desktop shortcut removal uses
`-Desktop -Remove`. Keep the checkout at its registered location and avoid
starting a second Hub while the login instance is running. Login startup requires
a logged-in user and does not restart failed processes. For startup before login,
use the separate [Task Scheduler deployment](docs/operations.md#windows-task-scheduler-example).
See [launcher options and startup details](docs/operations.md#windows-quick-start-and-login-startup)
for settings backups, wheel installs, script policy and moving the checkout.

## Documentation

- [Installation, services, logs, backup and rollback](docs/operations.md)
- [Configuration and CLI](docs/configuration.md)
- [Hub API contract](docs/api.md) and [backend behavior](backend.md)
- [History schema and migrations](docs/history-schema.md), [storage ADR](docs/adr/0002-hub-history-storage.md)
- [Internal alerts](docs/alerts.md) and [access boundaries](docs/security.md)
- [Measured resource budget and limitations](docs/resource-budget.md)
- [Architecture](docs/architecture.md), [Agent contract](docs/agent-contract.md), [ports](docs/ports.md)
- [Troubleshooting](docs/troubleshooting.md), [release/upgrade policy](docs/release.md), [CHANGELOG](CHANGELOG.md)
- [Brand provenance](docs/branding.md), [earlier Windows validation](docs/windows-soak.md)

## Development validation

```sh
python -m unittest discover -s tests -v
python -m compileall -q hub tests
node --check hub/web/app.js
node --check hub/web/devices.js
python scripts/check-package.py --output-dir /temporary/candidate-artifacts
```

CI covers Ubuntu/Windows × Python 3.9/3.13, isolated package install/rebuild,
synthetic smoke and platform configuration checks. Python package build tools are
not Hub runtime dependencies. Real registry files and operational logs stay private.
Application version, Hub HTTP schema 1, SQLite schema 4 and Agent API schemas are
independent contracts. See the release policy before upgrading or downgrading.

[Apache License 2.0](LICENSE).

## Export collected data

Export persisted measurements, events and gaps as CSV files in a versioned ZIP
using `python -m hub export`. See [History data exports](docs/data-export.md)
for commands, UTC time selection, limits and missing-data semantics.

The Dashboard **Data** tab downloads raw CSV ZIPs or daily/monthly reports.
Daily archives are opt-in with `--archive-dir PATH`: recognized raw ZIPs are kept
up to 90 days and daily summaries up to 365 days, sharing a default 10 GiB quota.
`python -m hub report --archive-dir PATH --month YYYY-MM --output report.zip`
builds a monthly report from retained daily summaries after raw History expires.
Missing samples and dates remain unknown. See the export guide for limits and
commands; enabling archives does not change database retention.
