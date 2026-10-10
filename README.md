# platmon-hub

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="hub/web/assets/brand/platmon-logo-dark.svg">
  <img src="hub/web/assets/brand/platmon-logo-light.svg" alt="platmon — approved C / Terminal P brand" width="300">
</picture>

Centralized, read-only fleet monitoring for Linux devices running [platmon](https://github.com/platmon/platmon).

> **Project status:** Fleet Dashboard preview implemented; no release package, production authentication, or long-term metrics database yet. Windows two-device fault isolation and a 30-minute two-node soak manually validated; large-fleet capacity and production security remain unverified.

`platmon` v1.0.0 is the independently deployed device agent. Each agent owns local hardware collection, CLI/Web UI, HTTP API and bounded in-memory history. The Hub reads existing published agent responses from an explicit registry, keeping per-node state, age and errors. It does not install/control agents, read their host files, or scan networks.

## Try the backend (Python 3.9+)

```sh
cp nodes.example.json nodes.json      # edit with explicitly approved agent endpoints and stable UUIDs
python3 -m hub --nodes nodes.json     # loopback 127.0.0.1:9787 by default
curl -fsS http://127.0.0.1:9787/api/nodes
# Open http://127.0.0.1:9787/ in a browser for Fleet Overview
```

The example contains **no active targets**. See [backend setup, API and security limitations](backend.md) before configuring a private registry. No third-party Python dependencies are required.

## Documents

- [Brand identity and asset provenance](docs/branding.md) — canonical platmon SVGs, dark/light variants, favicon and local static routes.
- [Default ports and collision diagnostics](docs/ports.md) — fixed Hub TCP 9787, Agent TCP 9797, bind failures, Windows/Linux checks.
- [Windows CI and 30-minute soak validation](docs/windows-soak.md) — cross-platform CI, Windows process CPU/RSS, poll duration and fleet status sampling.
- [Backend foundation operations](backend.md) — registry, runtime defaults, HTTP routes and known limitations.
- [Architecture and boundaries](docs/architecture.md) — components, polling, lifecycle, failure isolation, security, and milestones.
- [Agent API consumption contract](docs/agent-contract.md) — v1.0.0 HTTP schemas, identities and freshness.
- [MVP acceptance and verification plan](docs/mvp-acceptance.md) — contract scenarios and two-device field validation.
- [ADR-0001: HTTP Pull and Hub-owned identity](docs/adr/0001-http-pull-and-device-identity.md).

## Milestones

1. **Architecture & Contract** — merged in PR #1.
2. **Backend foundation** — this PR: registry, polling, state projection, tests and read-only API.
3. **Fleet overview** — this PR: static read-only browser page with node summary and details.
4. **Persistence & history** — long-term retention, gap-aware graph and optional alerts.

Upstream baseline: [platmon v1.0.0](https://github.com/platmon/platmon/releases/tag/v1.0.0), with `/api/status` and `/api/stats` schema 1 and `/api/observations` schema 2. Application versions and API schemas are independent.

[Apache License 2.0](LICENSE).
