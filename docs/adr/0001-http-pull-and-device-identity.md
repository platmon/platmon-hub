# ADR-0001: HTTP Pull from independent agents and Hub-owned node identity

- **Status:** Proposed (decision to be reviewed before implementation)
- **Date:** 2026-10-10
- **Scope:** platmon-hub initial fleet MVP (Jetson Orin Nano and Raspberry Pi)
- **Related:** [Architecture](../architecture.md), [Agent contract](../agent-contract.md), [MVP acceptance](../mvp-acceptance.md)

## Context

The separately released [platmon v1.0.0](https://github.com/platmon/platmon/releases/tag/v1.0.0) provides passive local collection and read-only HTTP APIs on Linux. It already runs independently of any fleet service, contains its own short-lived memory history, and has no HTTP authentication. Its random `instance_id` changes each time the service restarts; it does not advertise a globally persistent device identity.

A prospective central viewer must support at least two heterogeneous boards, handle independently missing sensors and node outages, and avoid adding runtime requirements or release coupling to platmon.

## Proposed decision

1. Maintain **two repositories** and independent releases: `platmon/platmon` (agent) and `platmon/platmon-hub` (central viewer).
2. Use explicit static **HTTP Pull** from Hub to the existing agent `/api/status`, `/api/stats`, and `/api/observations` endpoints. Do not add Push, remote configuration or other agent endpoints for the MVP.
3. Register each device explicitly in Hub with a **durable opaque `node_id`**, human-readable label and endpoint URL. IP address, hostname, `instance_id` and sensor IDs do not replace `node_id`.
4. Use **endpoint-specific schema gates**: v1.0.0 has schema 1 for status/stats and schema 2 for observations. Keep core and optional group failures separate; never infer a valid sample from HTTP 200 alone.
5. Bound timeouts, per-node concurrency and registry changes. Deploy only on a trusted management network or VPN; Hub's UI cannot become an arbitrary proxy or remote-control channel.

The implementation details (registry file/DB encoding, HTTP client framework, frontend stack) are intentionally **deferred** to implementation PRs; the invariants above are the reviewable contract.

## Alternatives considered

| Alternative | Advantage | Why not in the initial MVP |
| --- | --- | --- |
| Agent Push to Hub | Better for NAT/variable networks; can stream with fewer polls. | Requires agent protocol, delivery/replay/backpressure and authentication changes; increases v1.0.0 coupling. |
| Shared code/monorepo | Can reuse Python types and test helpers. | Couples releases and dependencies; hides cross-process API contract. |
| Use hostname/IP or `instance_id` as node ID | No registry identity to manage. | IP/hostname can move or collide; `instance_id` resets on every agent restart. |
| Full metrics stack (Prometheus + Grafana) immediately | Mature storage, querying and alerts. | Operational complexity exceeds two-node read-only overview; can evaluate for history phase. |

## Consequences and risks

- Hub can be upgraded or stopped without impacting agent collection. Explicit local network reachability is required; outbound-only or NAT-isolated nodes are deferred.
- Polling trades sampling resolution for operational simplicity. Hub cannot claim to observe every 1-second agent measurement with a 5-second poll. Capacity and HTTP cost must be measured before scaling claims.
- The Hub registry becomes the source of truth for logical node identity. Hardware replacement and same-host duplication need an operator-managed policy; there is no automatic physical attestation.
- Agent's unauthenticated HTTP API is safe only within a suitably restricted trust boundary; production remote deployment needs a separate security decision.
- The Hub must preserve partial/unavailable/stale semantics and never mistake Wi-Fi `null`, absent GPU or core `503` for network disconnection.

## Reconsider when

A deployment needs devices behind NAT, encrypted authenticated transport, significant fleet scale with expensive periodic reads, durable delivery during network outages, or remote configuration/control. Those requirements warrant a separate ADR and explicit upstream agent version negotiation.
