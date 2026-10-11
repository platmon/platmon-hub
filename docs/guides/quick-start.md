# Quick Start

[Documentation index](../README.md) · [User guide](README.md) · [Setup and operations](setup.md)

Requires **Git and Python 3.9+** on the Windows or Linux computer that will run Hub. No third-party Python dependencies, package installation, or frontend build are required. This first run uses an empty registry, so you can check the dashboard without any agents.

## 1. Get the source

Run in PowerShell on Windows or a shell on Linux:

```sh
git clone https://github.com/platmon/platmon-hub.git
cd platmon-hub
```

If you already have a checkout, open a terminal in its **repository root** instead: the folder containing `README.md`, `nodes.example.json`, and `hub/`. Run every Hub command below from that folder, not from `hub/` or `docs/`.

## 2. Start Hub

**Windows PowerShell:**

```powershell
python --version
python -m hub --nodes .\nodes.example.json
```

If Windows provides Python through the `py` launcher, use `py -3` in place of `python` in these commands and the setup guide. Check that the version is 3.9 or newer before starting Hub.

**Linux:**

```sh
python3 --version
python3 -m hub --nodes ./nodes.example.json
```

Keep this terminal open. A successful start logs a line containing (with a timestamp and INFO prefix):

```text
platmon-hub serving on 127.0.0.1:9787
```

## 3. Open and verify the dashboard

On the **same computer**, open [http://127.0.0.1:9787/](http://127.0.0.1:9787/). You should see the Fleet Overview with **0 registered devices** and “No nodes are registered.” This is expected: `nodes.example.json` contains `{"nodes": []}`. Open the server URL, not `hub/web/index.html` as a local file.

For an API check, open a **second terminal** while Hub keeps running.

**Windows PowerShell:**

```powershell
Invoke-RestMethod http://127.0.0.1:9787/healthz | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:9787/api/nodes | ConvertTo-Json
```

**Linux:**

```sh
curl -fsS http://127.0.0.1:9787/healthz
curl -fsS http://127.0.0.1:9787/api/nodes
```

The responses should be `{"status":"ok"}` and `{"schema_version":1,"nodes":[]}` (formatting may differ). Stop Hub with **Ctrl+C** in the first terminal.

## 4. Add your devices

Follow [Register agents and start monitoring](setup.md#register-agents-and-start-monitoring) to create or reuse a private registry, register already-running agents with the local CLI, and start Hub using that file. Valid registry edits then reload automatically by default. The example registry stays empty; Hub does not discover or install agents.

For file locations, see the [repository layout](../development/README.md#repository-layout). If startup fails, use [first-run troubleshooting](troubleshooting.md).

For a saved Windows launcher with History and the Devices tab, see [Windows quick start and login startup](operations.md#windows-quick-start-and-login-startup). For an installed CLI or supervised service, follow [installation and operations](operations.md).
