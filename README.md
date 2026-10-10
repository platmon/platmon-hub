# platmon-hub

Centralized, read-only fleet monitoring for Linux devices running [platmon](https://github.com/platmon/platmon).

> **Project status:** architecture/design proposal only. No Hub service, web dashboard, or deployable package has been implemented yet.

`platmon` v1.0.0 is the independently deployed device agent. Each agent owns local collection, its local Web UI, CLI, HTTP API, and bounded in-memory history. `platmon-hub` is a separate prospective service that will poll registered agents, preserve each node's identity and data freshness, and provide a fleet overview without changing or controlling the agents.

The first implementation target is **two nodes**: one Jetson Orin Nano and one Raspberry Pi. The design permits later scaling, but does not claim a tested fleet capacity.

## Design documents

- [Architecture and system boundaries](docs/architecture.md) — components, polling, lifecycle, failure isolation, security and milestones.
- [Agent API consumption contract](docs/agent-contract.md) — actual platmon v1.0.0 HTTP schemas, stable IDs, freshness, compatibility and interpretation rules.
- [MVP acceptance and verification plan](docs/mvp-acceptance.md) — scenarios for the first two devices and subsequent implementation PRs.
- [ADR-0001: HTTP Pull and Hub-owned device identity](docs/adr/0001-http-pull-and-device-identity.md) — proposed architectural decision and its trade-offs.

## Planned milestones

1. **Architecture & Contract** (this change): documentation only; no application or dependencies.
2. **Backend foundation:** persisted explicit node registry, bounded HTTP polling, node status projection, tests.
3. **Fleet overview:** single read-only page with node statuses, key resources and clear observation age.
4. **Persistence & history:** long-term time-series storage, retention and charting; optional alerts later.

## Upstream compatibility baseline

This design is grounded in the **platmon v1.0.0** published API, not implementation internals:

- [HTTP API](https://github.com/platmon/platmon/blob/v1.0.0/docs/api.md)
- [Low-frequency observations](https://github.com/platmon/platmon/blob/v1.0.0/docs/observations.md)
- [Recent history](https://github.com/platmon/platmon/blob/v1.0.0/docs/history.md)

The endpoint schema versions differ: `/api/stats` and `/api/status` are **schema 1**, while `/api/observations` is **schema 2** in v1.0.0. Application release versions and API schema versions must not be conflated.

## License

[Apache License 2.0](LICENSE).
