# platmon-hub

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/platmon/platmon-hub/main/hub/web/assets/brand/hub-logo-dark.svg">
  <img src="https://raw.githubusercontent.com/platmon/platmon-hub/main/hub/web/assets/brand/hub-logo-light.svg" alt="platmon hub" width="300">
</picture>

Fleet monitoring for independent [platmon](https://github.com/platmon/platmon) agents. Run Hub on Windows or Linux to view live readings, optional SQLite history, internal alerts, and data exports. Hub never installs or controls agents.

Supports platmon **1.0.0–1.3.0**, including GPU/NPU usage and independent battery/external-power observations. Opt-in full-publication raw capture needs platmon 1.3.0 or later. See the [Agent contract](https://github.com/platmon/platmon-hub/blob/main/docs/design/agent-contract.md).

**Status:** `1.1.1`. See [project status and validation limits](https://github.com/platmon/platmon-hub/blob/main/docs/README.md#project-status). HTTP is read-only by default; local web device management is opt-in. Keep the first run on loopback.

## Install

Requires **Python 3.9+** and [pipx](https://pipx.pypa.io/). Hub has no third-party runtime dependencies.

```sh
pipx install platmon-hub
platmon-hub --version
```

Start Hub with an empty registry in the current directory:

**Windows PowerShell:**

```powershell
Set-Content -Path nodes.json -Value '{"nodes": []}' -Encoding ascii
platmon-hub --nodes .\nodes.json
```

**Linux:**

```sh
echo '{"nodes": []}' > nodes.json
platmon-hub --nodes ./nodes.json
```

Keep the terminal running and open **[http://127.0.0.1:9787/](http://127.0.0.1:9787/)** on the same computer. Zero devices is expected. Stop with **Ctrl+C**. Update later with `pipx upgrade platmon-hub`.

This is a personal install. For a Linux service, a Windows startup task or an upgrade of a Hub that keeps history, follow [install and operate](https://github.com/platmon/platmon-hub/blob/main/docs/guides/operations.md): those use a dedicated venv and a backup before upgrading.

## Run from source

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

Open the same address as above. Next: [register agents](https://github.com/platmon/platmon-hub/blob/main/docs/guides/setup.md#register-agents-and-start-monitoring), or follow the [detailed Quick Start](https://github.com/platmon/platmon-hub/blob/main/docs/guides/quick-start.md) for expected output and API checks. Windows users with an existing registry can use the [saved launcher and login startup](https://github.com/platmon/platmon-hub/blob/main/docs/guides/windows-startup.md).

## Documentation

- [Documentation index](https://github.com/platmon/platmon-hub/blob/main/docs/README.md) — all guides and project status.
- [User guide](https://github.com/platmon/platmon-hub/blob/main/docs/guides/README.md) — setup, background execution, automatic startup, and operations.
- [Design reference](https://github.com/platmon/platmon-hub/blob/main/docs/design/README.md) — architecture, API, storage schema, and design decisions.
- [Proposed Agent replay and full-publication storage](https://github.com/platmon/platmon-hub/blob/main/docs/design/adr/0003-agent-replay-and-raw-publications.md).
- [Development and validation](https://github.com/platmon/platmon-hub/blob/main/docs/development/README.md) — source layout, local checks, and CI.

[Apache License 2.0](https://github.com/platmon/platmon-hub/blob/main/LICENSE).
