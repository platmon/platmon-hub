# Configuration and local administration

[Documentation index](../README.md) · [User guide](README.md)

All paths are explicit; keep private state outside the installation directory.
The runtime uses Python 3.9+ standard libraries. See [operations](operations.md)
for service accounts and filesystem permissions.

| Option | Default / behavior |
| --- | --- |
| `--nodes PATH` | Required strict JSON registry |
| `--bind ADDRESS` | `127.0.0.1`; retain loopback behind an authenticated proxy. IPv6 literals such as `::1` are supported |
| `--port PORT` | `9787`; fixed integer 1–65535, no automatic fallback |
| `--history-db PATH` | Disabled unless supplied; local SQLite database |
| `--raw-capture` | Disabled; opt-in replay persistence, requires history and explicit raw retention/quota |
| `--raw-retention-days DAYS` | Required for raw capture; finite positive, at most 365 days; based on receipt time |
| `--raw-quota-mib MIB` | Required for raw capture; 1–1048576; encoded raw rows plus envelope/bookkeeping, not SQLite file size |
| `--raw-queue-mib MIB` | 8; 3–64 MiB encoded replay queue, at most 16 pending commands/pages |
| `--handoff-spool PATH` | Opt-in downstream handoff ([ADR-0004](../design/adr/0004-downstream-data-handoff.md)); an existing writable directory with hard links; requires raw capture and the two options below |
| `--handoff-spool-quota-mib MIB` | Required for handoff; 1–1048576; segment production pauses while the spool holds this much |
| `--handoff-spool-retention-days DAYS` | Required for handoff; keeps acknowledged segments; at most `--raw-retention-days` |
| `--handoff-start now\|existing` | `now`; applies when handoff is first enabled: exclude existing rows (recorded as `not_handed_off`) or hand them off by backfill |
| `--archive-dir PATH` | Opt-in daily archive directory; requires history; see [exports and reports](data-export.md) |
| `--archive-retention-days DAYS` | 90; raw ZIP retention upper bound, subject to quota |
| `--archive-summary-retention-days DAYS` | 365; daily-summary retention upper bound, subject to quota |
| `--archive-quota-mib MIB` | 10240; shared quota for recognized raw ZIPs and summaries |
| `--web-admin` | Opt-in local web device management; requires loopback and registry reload |
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
platmon-hub nodes --file /private/nodes.json set-capture NODE_UUID sampled
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

`--web-admin` enables the Dashboard **Devices** management tab on the local Hub
page without a token or login. It trusts all local users/processes, requires
loopback binding and registry reload; other HTTP write routes remain disabled.
The Hub account needs write permission for the registry directory and backup. See
[web management](web-registration.md) for Origin/header checks and viewer-proxy
restrictions. Omit the option to retain read-only HTTP behavior.
