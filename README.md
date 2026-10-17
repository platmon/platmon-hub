# platmon-hub

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="hub/web/assets/brand/hub-logo-dark.svg">
  <img src="hub/web/assets/brand/hub-logo-light.svg" alt="platmon hub" width="300">
</picture>

Fleet monitoring for independent [platmon](https://github.com/platmon/platmon) agents. Run Hub on Windows or Linux to view live readings, optional SQLite history, internal alerts, and data exports. Hub never installs or controls agents.

Supports platmon **1.0.0–1.3.0**, including GPU/NPU usage and independent battery/external-power observations. Opt-in full-publication raw capture needs platmon 1.3.0 or later. See the [Agent contract](docs/design/agent-contract.md).

**Status:** `1.1.0`. See [project status and validation limits](docs/README.md#project-status). HTTP is read-only by default; local web device management is opt-in. Keep the first run on loopback.

## Quick Start

Requires **Git and Python 3.9+**. Running from source needs no third-party Python packages or frontend build.

```sh
git clone https://github.com/platmon/platmon-hub.git
cd platmon-hub
```

From the repository root, start Hub with the empty example registry:

**Windows PowerShell** (use `py -3` if that is your Python launcher):

```powershell
python -m hub --nodes .\nodes.example.json
```

**Linux:**

```sh
python3 -m hub --nodes ./nodes.example.json
```

Keep the terminal running and open **[http://127.0.0.1:9787/](http://127.0.0.1:9787/)** on the same computer. Zero devices is expected. Stop with **Ctrl+C**.

Next: [register agents](docs/guides/setup.md#register-agents-and-start-monitoring), or follow the [detailed Quick Start](docs/guides/quick-start.md) for expected output and API checks. Windows users with an existing registry can use the [saved launcher and login startup](docs/guides/windows-startup.md).

## Documentation

- [Documentation index](docs/README.md) — all guides and project status.
- [User guide](docs/guides/README.md) — setup, background execution, automatic startup, and operations.
- [Design reference](docs/design/README.md) — architecture, API, storage schema, and design decisions.
- [Proposed Agent replay and full-publication storage](docs/design/adr/0003-agent-replay-and-raw-publications.md).
- [Development and validation](docs/development/README.md) — source layout, local checks, and CI.

[Apache License 2.0](LICENSE).
