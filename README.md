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

Use **Add device** in the Dashboard after enabling [local web registration](docs/web-registration.md), or add an approved device from a local terminal:

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
component. HTTP is read-only by default. Opt-in [local web device registration](docs/web-registration.md) uses a protected administrator token.

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
python scripts/check-package.py --output-dir /temporary/candidate-artifacts
```

CI covers Ubuntu/Windows × Python 3.9/3.13, isolated package install/rebuild,
synthetic smoke and platform configuration checks. Python package build tools are
not Hub runtime dependencies. Real registry files and operational logs stay private.
Application version, Hub HTTP schema 1, SQLite schema 4 and Agent API schemas are
independent contracts. See the release policy before upgrading or downgrading.

[Apache License 2.0](LICENSE).
