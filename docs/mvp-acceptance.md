# platmon-hub MVP acceptance and verification

[Documentation index](README.md) · [Development checks](development.md)

**Status:** Regression and release checklist for the implemented preview. Automated tests are present; recorded manual evidence is linked below. This document does not certify a complete release acceptance pass.

**Targets:** one Jetson Orin Nano and one Raspberry Pi running [platmon v1.0.0](https://github.com/platmon/platmon/releases/tag/v1.0.0).

**Related:** [Architecture](architecture.md), [API contract](agent-contract.md), [ADR-0001](adr/0001-http-pull-and-device-identity.md).

This checklist covers the original Fleet contract. The implemented candidate also includes History, registry hot reload, events, internal alerts, packaging, and data exports/reports. For those features and current rollout gates, use the [release policy](release.md), [schema](history-schema.md), [API](api.md), and [export guide](data-export.md).

## 1. Verification sources

| Area | Existing coverage or procedure | Evidence boundary |
| --- | --- | --- |
| Registry, protocol, state, scheduler, and HTTP client/API | [test_backend.py](../tests/test_backend.py) | Synthetic fixtures and local HTTP servers. |
| Dashboard metrics, assets, and rendering safeguards | [test_dashboard.py](../tests/test_dashboard.py) | Projection and source/HTTP checks; also inspect the browser manually. |
| Listener errors and CLI port validation | [test_ports.py](../tests/test_ports.py) | Controlled bind failures and CLI checks. |
| Poll duration metadata | [test_poll_timing.py](../tests/test_poll_timing.py) | Fake-clock timing fixtures, not hardware performance. |
| Two-device hardware behavior | Hardware procedure below and [Windows soak](windows-soak.md) | Recorded evidence applies only to that equipment, build, and workload. |

Run automated checks with the [development guide](development.md#local-checks). The scenarios below are regression and acceptance requirements, not a statement that every scenario has a current passing hardware result.

## 2. Contract scenarios

| ID | Scenario | Required observation |
| --- | --- | --- |
| C01 | Status 200, schema 1, `ready=true`, `state=ready`; stats 200 schema 1 with sample 42 | Hub shows `live`, data age and sample identity. |
| C02 | Status 200, schema 1, `state=degraded`, `ready=true`; optional collector `partial`/`error` | `partial`, details visible; core values still current. |
| C03 | Status 200, `state=starting`, `ready=false`, sample null | `starting`, no fabricated numbers; host not Offline. |
| C04 | Status 200, `state=stale`, `ready=false`, last sample present | `stale`, retained data explicitly old; host not Offline. |
| C05 | Status 200 ready true, then stats 503 from collection race | No current snapshot; annotate stale/error and recover at next poll, not transport Offline. |
| C06 | Repeated status/stats polls return exactly the same `(instance_id, sequence)` | No duplicate measurement publication; age continues increasing. |
| C07 | Agent restart gives new `instance_id` and `sequence=1` | Hub preserves `node_id` but treats new process/sample generation as new. |
| C08 | Out-of-order lower `sequence` for same `instance_id` | Older sample does not overwrite latest sample. |
| C09 | Network error once; network error twice; then valid status | First is uncertain/stale with poll error; second `offline`; next success restores native state. Other nodes continue. |
| C10 | Reachable HTTP 500 or invalid JSON | Visible server/protocol `error`, not falsely `offline`; last values not Live. |
| C11 | Unexpected/missing required status/stats schema; unknown extra keys on supported schema | Required endpoint `incompatible` vs safe acceptance of additive keys. |
| C12 | Observations 200 with schema **2**, groups `{}`; later Wi-Fi `connected=null` | Empty groups valid; null is unknown (not disconnected). |
| C13 | Observations stale `storage` group with nonempty old `data`, or 404/mismatched optional schema | Group not current; optional problem isolated from core state. |
| C14 | Pi without a GPU or agent collector `unavailable`; CPU warmup; missing metrics | Render N/A/unknown rather than zero or a false alert. |
| C15 | Agent clock jumps, Hub clock changes, or Hub caches a response past `stale_after_ms` + poll interval + deadlines; a healthy node polled on schedule never flips to Stale between polls | Freshness uses elapsed age + Hub monotonic delta; never wall-clock difference to assert Live. |
| C16 | Same display hostname or changed IP on two registered devices | Distinct permanent `node_id`s preserved; no implicit node merge. |
| C17 | One node hangs or times out during polling | Other node progresses while capacity is available; measure delays and verify no overlap, including the documented DNS limits. |
| C18 | Malicious/oversized response, strange sensor label or arbitrary URL in a UI field | Bounded response handling; HTML escaped; no arbitrary agent/network fetch. |
| C19 | `/api/history` 404 or agent restart clears it | Core state unaffected; no implication of durable preexisting history. |
| C20 | `/api/status` body reports `state=ready` but `ready=false` (or wrong type) | Reject as protocol error; do not claim Live. |

Build fixture payloads from the published v1.0.0 contract; distinguish agent **schema 1** core from **schema 2** observations. A passing mock fixture does not replace testing against actual boards.

## 3. Hardware smoke run

1. Record the **actual installed agent versions, configurations, endpoints and connectivity** for Orin Nano and Pi; verify each agent independently using its existing `/api/status`, `/api/stats` and `/api/observations` with an operator-approved LAN route.
2. Start Hub with two **explicitly registered** nodes. Verify both persistent IDs and display names across one Hub restart. Keep the same IDs after one agent restart, even though its `instance_id` changes.
3. Observe at least 30 minutes of read-only polls with the current 5 s core / 10 s observations intervals (plus runtime, jitter, and backoff); record actual poll frequency, p50/p95 durations, retries, HTTP failures, total CPU/RSS and network activity. Do not declare an unmeasured performance pass.
4. On Orin, confirm available Jetson-only values are preserved with their units; on Pi, confirm unsupported values remain N/A, not critical or zero.
5. Stop **one** agent, or isolate its network path with explicit operator approval; verify other device keeps updating. Restore it and verify state recovery and a new `instance_id` after restart.
6. Force/mock partial collector failure and slow/stale observations; verify the fleet UI displays separate core and optional diagnostic states, with last-known data visibly aged.
7. Verify Hub shutdown/unavailability has **no observable effect** on either agent's local collection or local UI.
8. Confirm no agent control writes, no arbitrary-target scans, no public internet endpoint and no unauthorized exposure of unauthenticated port 9797.

Do not intentionally break a production device to satisfy tests. Use fixtures for destructive/failure injection when a live-device experiment is not authorized.

## 4. Nonfunctional acceptance before an MVP release

- **Isolation:** measure the operating bounds for slow/unreachable agents, saturation, and shutdown. Verify the concurrency cap and no-overlap behavior; the current socket timeouts do not provide strict DNS cancellation (see [runtime limits](architecture.md#default-polling-and-state-semantics)).
- **Fidelity:** sample identity, elapsed freshness, nulls, data gaps, per-group state and schema mismatch are visible and not silently normalized away.
- **Recovery:** static registry survives Hub restart; process `instance_id` changes do not create duplicate managed nodes.
- **Read-only:** Hub only performs predeclared GETs to preconfigured agent endpoints; registration is an operator action, not a browser-side proxy.
- **Observability:** log node ID, poll result/error category and timings without logging arbitrary payloads or sensitive network error detail to the UI.
- **Documentation:** installation, trust-boundary restrictions, limits and any measured capacity are documented with reproducible evidence.

## 5. Candidate rollout gates

Registry hot reload, History, events, internal alerts, packaging, and reports are implemented. The [release policy](release.md#evidence-and-remaining-acceptance-gates) and [resource budget](resource-budget.md) track current evidence and limits. Service reboot/account permissions and a longer operational pilot remain. Do not interpret the older two-device soak or this baseline checklist as acceptance of every later feature.
