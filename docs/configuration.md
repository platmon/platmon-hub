# Configuration and local administration

All paths are explicit; keep private state outside the installation directory.
The runtime uses Python 3.9+ standard libraries. See [operations](operations.md)
for service accounts and filesystem permissions.

| Option | Default / behavior |
| --- | --- |
| `--nodes PATH` | Required strict JSON registry |
| `--bind ADDRESS` | `127.0.0.1`; retain loopback behind an authenticated proxy |
| `--port PORT` | `9787`; fixed integer 1–65535, no automatic fallback |
| `--history-db PATH` | Disabled unless supplied; local SQLite database |
| `--history-retention-days DAYS` | 7; finite positive number, core and observations |
| `--event-retention-days DAYS` | 30; finite positive number |
| `--alerts PATH` | Disabled unless supplied; requires history; see [rules](alerts.md) |
| `--no-registry-reload` | Opt out of automatic validated file reload |
| `--log-file PATH` | Otherwise stderr; UTF-8 rotating file, 2 MiB × 4 files |
| `--version` | Print application version and exit |

Core/observations polling remains 5/10 seconds, with status/stats/observations
request timeouts 2/2/3 seconds, 16 workers, one in-flight request sequence per node
and 512 KiB response limits. These are implementation defaults, not extra CLI flags.
History admission uses a bounded 2,048-record queue; overflow drops history without
waiting in polling. Check diagnostics before treating the database as complete.

## Registry CLI

```sh
platmon-hub nodes --file /private/nodes.json validate
platmon-hub nodes --file /private/nodes.json list
platmon-hub nodes --file /private/nodes.json add --name Example --endpoint http://192.0.2.1:9797 --disabled
platmon-hub nodes --file /private/nodes.json rename NODE_UUID "New name"
platmon-hub nodes --file /private/nodes.json set-endpoint NODE_UUID http://192.0.2.2:9797
platmon-hub nodes --file /private/nodes.json enable NODE_UUID
platmon-hub nodes --file /private/nodes.json disable NODE_UUID
platmon-hub nodes --file /private/nodes.json remove NODE_UUID
```

Substitute an approved endpoint and the UUID returned by `add`. The parent directory
must exist. Add creates the file when absent; other commands require an existing
registry. Changes use a cooperative OS file lock, validation, a `.bak` copy of the
previous contents and atomic replacement. Protect backups with the registry.
Concurrent manual editors do not participate in the CLI lock; avoid mixing them.

The root has only `nodes`; each entry contains `node_id`, `name`, `endpoint` and
`enabled`. IDs are canonical UUIDs. Origins must be plain HTTP without credentials,
paths, query or fragment; duplicate IDs/endpoints and more than 256 entries are
rejected. This parser limit is not a measured capacity guarantee. Name and endpoint
changes preserve the UUID. Disable stops scheduling; remove does not erase history.
Invalid reloads preserve the last accepted registry. HTTP registry writes are disabled unless local web administration is explicitly enabled.

## Opt-in local web device management

`--admin-token-file /private/admin-token.txt` enables the Dashboard **Devices**
management tab on the local Hub page with bearer authorization. It requires loopback binding
and registry reload; other HTTP write routes remain disabled. The Hub account
needs write permission for the registry directory and backup. See
[web registration](web-registration.md) for protected token creation, Windows ACLs,
CSRF/Origin rules, rotation and viewer-proxy restrictions.
