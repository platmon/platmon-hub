# Design reference

[Documentation index](../README.md) · [User guide](../guides/README.md)

These documents explain Hub architecture, interfaces, persistence, and design decisions. For installation, startup, and day-to-day operation, use the [user guide](../guides/README.md).

| Reference | Contents |
| --- | --- |
| [Architecture](architecture.md) | Implemented components, polling, persistence, reload, and failure boundaries. |
| [Hub API](api.md) | Live, history, registry diagnostics, events, alerts, and data routes. |
| [Agent contract](agent-contract.md) | Pinned platmon schemas and identity/freshness rules. |
| [History schema](history-schema.md) | Storage versions, writer semantics, migration, and recovery. |
| [ADR-0001](adr/0001-http-pull-and-device-identity.md) | Independent agents, HTTP Pull, and Hub-owned identity. |
| [ADR-0002](adr/0002-hub-history-storage.md) | Isolated SQLite writer, admission, continuity, and durability decisions. |
