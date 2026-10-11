# Documentation

[Project README](../README.md) · [Quick Start](guides/quick-start.md)

Choose a directory by what you need to do. Commands for a source checkout run from the repository root unless stated otherwise; Markdown links resolve relative to their document.

| Directory | Contents |
| --- | --- |
| [guides/](guides/README.md) | User instructions: setup, configuration, background execution, automatic startup, operations, and troubleshooting. |
| [design/](design/README.md) | Architecture, Hub API, Agent contract, History schema, and architecture decisions in `design/adr/`. |
| [development/](development/README.md) | Development, release policy, acceptance checks, branding, and measured evidence in `development/benchmarks/`. |

For Windows background execution or automatic startup, start with the [startup options](guides/README.md#background-execution-and-automatic-startup).

## Project status

The `0.2.0rc1` candidate implements Fleet monitoring, optional SQLite history, registry management, events, internal alerts, packaging, data exports, and reports. The Windows saved launcher enables History and local web management; a plain `python -m hub --nodes ...` start enables neither unless their options are supplied.

History is opt-in with `--history-db`; alerts additionally need `--alerts`. Defaults retain metrics for seven days and events for 30 days. Automatic archives are separately opt-in with `--archive-dir`, with raw ZIP and daily-summary retention bounded by quota. See [configuration](guides/configuration.md) and [exports](guides/data-export.md) for exact options and limits.

Three real devices passed short read-only collection/restart checks on Linux and native Windows. Synthetic 100-node and actual three-device 90-minute runs are documented in the [resource budget](development/resource-budget.md) and [extended soak report](development/benchmarks/long-soak.md). These results do not establish unrestricted production capacity.

Service reboot/ACL acceptance and a longer operational pilot remain before production rollout. A candidate version or merged PR does not imply a release tag or deployment. Application version, Hub HTTP schema, SQLite schema, and Agent endpoint schemas are separate contracts; follow the [release policy](development/release.md).
