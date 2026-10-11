# Hub listener port policy and bind-failure diagnosis

[Documentation index](README.md) · [Setup and operations](backend.md#access-boundary)

## Defaults and boundary

| Component | Default TCP listener | Where it runs |
| --- | --- | --- |
| platmon Agent | `9797` | Each Linux device (Jetson Orin Nano, Raspberry Pi, etc.) |
| platmon-hub | `9787` | Windows or Linux Hub host |

Hub binds to **`127.0.0.1:9787`** by default. Open the dashboard using `http://127.0.0.1:9787/`. Do not expose the Hub directly to an untrusted network: this MVP has **no authentication or TLS**. A non-loopback bind needs an operator-managed authenticated, encrypted reverse proxy or other approved protection.

Run Hub commands from the **repository root**, after creating your registry with the [setup guide](backend.md#register-agents-and-start-monitoring). Choose a different port explicitly:

```powershell
# Windows PowerShell (use py -3 instead of python if needed)
python -m hub --nodes "$env:LOCALAPPDATA\platmon-hub\nodes.json" --port 9798
```

```sh
# Linux
python3 -m hub --nodes "$HOME/.config/platmon-hub/nodes.json" --port 9798
```

For the empty-registry [Quick Start](quick-start.md), use `./nodes.example.json` in place of the private registry path. The Hub **never** scans for free ports, falls back to a different port, or accepts ephemeral `--port 0`. The operator must update browser URLs, proxies and service configuration consistently after changing the port.

`9787` is in the IANA **User Ports** range (1024–49151), **not an exclusive assignment to platmon-hub**. IANA service registration does not guarantee that the port is free on a given host, and an unregistered port would not guarantee exclusivity either. The successful Windows two-agent, 30-minute local soak established availability **on that test host at the time**, not universal freedom from collisions.

IANA registry: https://www.iana.org/assignments/service-names-port-numbers/
IANA User Ports definition: https://www.rfc-editor.org/rfc/rfc6335

## What happens when a port cannot be bound?

Startup attempts a **single** `HubServer((bind, port), fleet)` bind. For `EADDRINUSE` (Windows WSAEADDRINUSE 10048) or `EACCES`/`EPERM` (Windows WSAEACCES 10013), startup terminates with a message identifying the requested address/port and offering collision/permission diagnostics. It does **not** silently move to another port. Other unexpected socket errors are surfaced unchanged. The Hub never modifies Windows exclusions, firewall policy or running services.

An occupied socket and a reserved/excluded port are different failure modes. A permission or reservation error is only a possible cause to investigate; the message is not proof of a specific reservation.

## Windows PowerShell

With the Hub **stopped**, check for an existing listener:

```powershell
Get-NetTCPConnection -LocalPort 9787 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalAddress, LocalPort, OwningProcess
```

Find its process:

```powershell
Get-NetTCPConnection -LocalPort 9787 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue } |
  Select-Object Id, ProcessName
```

Check OS port exclusions and configured dynamic ranges (both IPv4 and IPv6):

```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
netsh interface ipv6 show excludedportrange protocol=tcp
netsh interface ipv4 show dynamicportrange protocol=tcp
netsh interface ipv6 show dynamicportrange protocol=tcp
```

An empty listener search does not prove that a port is bindable: Windows port exclusions, address family, permissions and other socket policies can still matter. When the Hub **is running**, seeing its own `python.exe` PID listening on 9787 is expected. Do **not** delete Windows port exclusions or terminate other processes automatically to make room for Hub.

Microsoft command reference: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/netsh-interface

## Linux

With Hub stopped, use `ss` to identify a conflicting TCP listener:

```sh
ss -ltnp '( sport = :9787 )'
```

If permission prevents process-name reporting, retry the inspection with the required administrative privileges; do not automatically kill the reported process.

## Resolution

1. Verify the listener belongs to a known service and that the bind address overlaps the Hub's requested interface (`127.0.0.1` by default).
2. If the port is in use, coordinate with the service owner or configure Hub with an explicit alternative `--port`.
3. If binding is denied without a visible listener, inspect Windows excluded port ranges or OS permission restrictions. Never alter security policy just to make a test pass.
4. Restart Hub with the chosen fixed port and verify `GET /healthz` and `GET /api/nodes` on that port.

If `--port 9798` is used, the browser URL becomes `http://127.0.0.1:9798/`; the Agent endpoints in `nodes.json` **remain on 9797** unless the Agents themselves were separately configured differently.
