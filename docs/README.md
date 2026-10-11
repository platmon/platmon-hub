# Documentation

[Project README](../README.md) · [Quick Start](quick-start.md)

Start with an empty dashboard, then register your agents. Commands for a source checkout run from the repository root unless stated otherwise; Markdown links resolve relative to their document. Packaged installations and service paths are explained in the operations guide.

## Use and operate Hub

| Guide | Contents |
| --- | --- |
| [Quick Start](quick-start.md) | Prerequisites, Windows/Linux startup, expected output, and API checks. |
| [Setup and backend behavior](backend.md) | Device registration, registry reload, dashboard, and persistence behavior. |
| [Configuration and CLI](configuration.md) | Options, private state, registry validation, and local administration. |
| [Local web device management](web-registration.md) | Devices tab, preview/import, and the opt-in local administration boundary. |
| [Installation and operations](operations.md) | Wheel installation, Windows launcher/login startup, services, logs, backup, and rollback. |
| [Ports](ports.md) | Default listeners and Windows/Linux bind diagnostics. |
| [Troubleshooting](troubleshooting.md) | Startup, connectivity, history, registry, and alert failures. |
| [Security](security.md) | Local access boundary, optional TLS/authentication proxy, and resource controls. |
| [Data exports and reports](data-export.md) | CSV downloads, daily archives, retained summaries, and monthly reports. |
| [Internal alerts](alerts.md) | Rule evaluation, unknown data, transitions, and restoration. |

## Develop and verify

| Guide | Contents |
| --- | --- |
| [Development](development.md) | Source layout, local checks, package validation, and CI. |
| [Release policy](release.md) / [Changelog](../CHANGELOG.md) | Candidate artifacts, upgrade compatibility, evidence, and remaining rollout gates. |
| [Resource budget](resource-budget.md) | Measured limits, actual-device checks, and capacity recommendations. |
| [Benchmark index](benchmarks/README.md) | Reproduction commands and [synthetic plan](benchmarks/plan.md). |
| [Extended soak](benchmarks/long-soak.md) / [plan](benchmarks/long-soak-plan.md) | 90-minute runs, investigations, and remaining evidence. |
| [Earlier Windows soak](windows-soak.md) | Two-device 30-minute procedure and the scope of its recorded evidence. |
| [Fleet acceptance checklist](mvp-acceptance.md) | Baseline contract scenarios and hardware regression checks. |
| [Brand assets](branding.md) | Provenance, source paths, browser URLs, and update rules. |

## Understand the design

| Reference | Contents |
| --- | --- |
| [Architecture](architecture.md) | Implemented components, polling, persistence, reload, and failure boundaries. |
| [Hub API](api.md) | Live, history, registry diagnostics, events, alerts, and data routes. |
| [Agent contract](agent-contract.md) | Pinned platmon schemas and identity/freshness rules. |
| [History schema](history-schema.md) | Storage versions, writer semantics, migration, and recovery. |
| [ADR-0001](adr/0001-http-pull-and-device-identity.md) | Independent agents, HTTP Pull, and Hub-owned identity. |
| [ADR-0002](adr/0002-hub-history-storage.md) | Isolated SQLite writer, admission, continuity, and durability decisions. |

## Project status

The `0.2.0rc1` candidate implements Fleet monitoring, optional SQLite history, registry management, events, internal alerts, packaging, data exports, and reports. The Windows saved launcher enables History and local web management; a plain `python -m hub --nodes ...` start enables neither unless their options are supplied.

History is opt-in with `--history-db`; alerts additionally need `--alerts`. Defaults retain metrics for seven days and events for 30 days. Automatic archives are separately opt-in with `--archive-dir`, with raw ZIP and daily-summary retention bounded by quota. See [configuration](configuration.md) and [exports](data-export.md) for exact options and limits.

Three real devices passed short read-only collection/restart checks on Linux and native Windows. Synthetic 100-node and actual three-device 90-minute runs are documented in the [resource budget](resource-budget.md) and [extended soak report](benchmarks/long-soak.md). These results do not establish unrestricted production capacity.

Service reboot/ACL acceptance and a longer operational pilot remain before production rollout. A candidate version or merged PR does not imply a release tag or deployment. Application version, Hub HTTP schema, SQLite schema, and Agent endpoint schemas are separate contracts; follow the [release policy](release.md).
