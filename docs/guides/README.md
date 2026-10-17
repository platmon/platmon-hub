# User guide

[Documentation index](../README.md) · [Design reference](../design/README.md)

Start with the [Quick Start](quick-start.md), then [register your agents](setup.md#register-agents-and-start-monitoring). Run source-checkout commands from the repository root, even though these guides live in `docs/guides/`.

## Background execution and automatic startup

Choose one Windows startup method. For a personal Windows PC, start with the login shortcut guide. If you already registered it, use [Start Hub now](windows-startup.md#3-start-hub-now).

| Goal | Instructions |
| --- | --- |
| Save Windows launcher settings and start at user login | [Windows launcher and login startup](windows-startup.md); the console window is minimized. |
| Run Windows in the background at boot, before login | [Windows Task Scheduler](windows-task-scheduler.md); registration requires administrator rights. |
| Run Linux as a service | [systemd](operations.md#linux-systemd-example). |

## Guides

| Guide | Contents |
| --- | --- |
| [Quick Start](quick-start.md) | Prerequisites, Windows/Linux startup, expected output, and API checks. |
| [Setup and backend behavior](setup.md) | Device registration, registry reload, dashboard, and persistence behavior. |
| [Configuration and CLI](configuration.md) | Options, private state, registry validation, and local administration. |
| [Local web device management](web-registration.md) | Devices tab, preview/import, and the opt-in local administration boundary. |
| [Windows login startup](windows-startup.md) | One registry, login shortcut registration, starting now, and stopping Hub. |
| [Windows Task Scheduler](windows-task-scheduler.md) | Separate administrator-managed deployment for startup before login. |
| [Installation and operations](operations.md) | Wheel installation, service guides, logs, backup, and rollback. |
| [Ports](ports.md) | Default listeners and Windows/Linux bind diagnostics. |
| [Troubleshooting](troubleshooting.md) | Startup, connectivity, history, registry, and alert failures. |
| [Security](security.md) | Local access boundary, optional TLS/authentication proxy, and resource controls. |
| [Full-publication replay](replay.md) | Opt-in raw capture, coverage, retention/quota and JSONL export. |
| [Data exports and reports](data-export.md) | CSV downloads, daily archives, retained summaries, and monthly reports. |
| [Internal alerts](alerts.md) | Rule evaluation, unknown data, transitions, and restoration. |
