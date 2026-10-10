# platmon-hub

Centralized, read-only fleet monitoring for Linux devices running [platmon](https://github.com/platmon/platmon).

> **Project status:** Backend foundation implemented for review; no Fleet Web UI, release package, production authentication, or long-term metrics database yet. Not production validated.

`platmon` v1.0.0 is the independently deployed device agent. Each agent owns local hardware collection, CLI/Web UI, HTTP API and bounded in-memory history. The Hub reads existing published agent responses from an explicit registry, keeping per-node state, age and errors. It does not install/control agents, read their host files, or scan networks.

## Try the backend (Python 3.9+)

```sh
cp nodes.example.json nodes.json      # edit with explicitly approved agent endpoints and stable UUIDs
python3 -m hub --nodes nodes.json     # loopback 127.0.0.1:9787 by default
curl -fsS http://127.0.0.1:9787/api/nodes
```

The example contains **no active targets**. See [backend setup, API and security limitations](backend.md) before configuring a private registry. No third-party Python dependencies are required.

## Documents

- [Backend foundation operations](backend.md) — registry, runtime defaults, HTTP routes and known limitations.
- [Architecture and boundaries](docs/architecture.md) — components, polling, lifecycle, failure isolation, security, and milestones.
- [Agent API consumption contract](docs/agent-contract.md) — v1.0.0 HTTP schemas, identities and freshness.
- [MVP acceptance and verification plan](docs/mvp-acceptance.md) — contract scenarios and two-device field validation.
- [ADR-0001: HTTP Pull and Hub-owned identity](docs/adr/0001-http-pull-and-device-identity.md).

## Milestones

1. **Architecture & Contract** — merged in PR #1.
2. **Backend foundation** — this PR: registry, polling, state projection, tests and read-only API.
3. **Fleet overview** — graphical device summary and detail.
4. **Persistence & history** — long-term retention, gap-aware graph and optional alerts.

Upstream baseline: [platmon v1.0.0](https://github.com/platmon/platmon/releases/tag/v1.0.0), with `/api/status` and `/api/stats` schema 1 and `/api/observations` schema 2. Application versions and API schemas are independent.

[Apache License 2.0](LICENSE).
