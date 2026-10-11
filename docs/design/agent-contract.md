# platmon agent consumption contract for Hub

[Documentation index](../README.md) · [Design reference](README.md) · [Architecture](architecture.md) · [Hub API](api.md)

**Status:** Agent contract baseline consumed by the current Hub preview. **Source of truth:** the tagged [platmon v1.2.0 HTTP API](https://github.com/platmon/platmon/blob/v1.2.0/docs/api.md), [observations](https://github.com/platmon/platmon/blob/v1.2.0/docs/observations.md) and [history](https://github.com/platmon/platmon/blob/v1.2.0/docs/history.md). All links are pinned to the agent release, not its evolving `main` branch. This document does **not** introduce or change endpoints in platmon.

This document describes upstream semantics and the Hub consumption policy. The implemented health/freshness checks are in [hub/protocol.py](../../hub/protocol.py) and [hub/state.py](../../hub/state.py); it is not a claim that every optional upstream field is validated. Hub-facing response fields are documented separately in the [Hub API reference](api.md).

The baseline tag resolves to commit `26eae6e975ce184e713418dd2858b56e50d3ab90`.
Versions 1.1.0 and 1.2.0 are additive: 1.0.0 agents remain supported without NPU
or battery fields. Hub does not require a global application-version handshake.

## 1. Compatibility matrix

| Agent resource | v1.2.0 response behavior | Schema | Hub consumer policy |
| --- | --- | --- | --- |
| `GET /api/status` | HTTP 200 even when no current snapshot; inspect `ready` and `state`. No collection triggered. | `1` | **Required** for node health and collection progress; cannot equate HTTP 200 to healthy. |
| `GET /api/stats` | HTTP 200 with current snapshot; **503** for no current data (including stale). Requests can wait up to 5 s for the first sample. | `1` | Read only when `/api/status` indicates `ready=true`; missing current data is **not** transport offline. |
| `GET /api/observations` | HTTP 200 and `groups` map (can be `{}`); reads the independent slow publishers; no collection triggered. | **`2`** | Poll on independent low cadence; handle group absence, individual staleness and tri-state Wi-Fi. Failure is not core failure. |
| `GET /api/history` | HTTP 200 with bounded in-memory series if enabled; **404** when disabled. Restart clears history. | `1` | Not polled. Hub persistence stores its own accepted responses; Agent history is not a durable backfill. |
| `GET /text`, `GET /` | Human-readable output or local Web UI (if enabled). | N/A | Not a machine contract for Hub. |

All agent API responses above carry `Cache-Control: no-store` in v1.2.0; Hub must not rely on intermediary caches. Default agent port is 9797, but it is configurable and must be stored with each node's endpoint. The API has **no authentication**: a trusted network/VPN is a prerequisite, not an authorization feature.

The **application release version `1.2.0` is not an endpoint schema version**. Hub independently validates the response schema **per endpoint**. It must not assume `/api/observations` schema 1 because `/api/stats` is schema 1.

## 2. Required core health fields

`/api/status` represents the **current collector process state**, not just the state of the last successful sample:

- `schema_version: 1`.
- `instance_id`: randomly generated UUID for this running service instance, replaced after agent restart.
- `ready` is strictly boolean; `state` is one of `ready`, `degraded`, `starting`, `stale`.
- `sample` may be `null`, including before the first successful publication. If present, it describes the **last good sample**, even if now stale.
- The status `sample` includes elapsed age fields such as `data_age_ms` but does **not** include `stale_after_ms`; use the reported `state`/`ready` for status health. The separate `/api/stats` `sample` includes its `stale_after_ms` for checking cached metric freshness.
- `last_attempt` records recent collection success/failure independently of the last good published sample; `collecting_for_ms` may grow during a blocked read without proving progress.
- `levels` supplies agent-selected warning/critical thresholds, including the board-specific effective temperature defaults. These are presentation thresholds; Hub must not reinterpret them as proof of service failure.

Native states and their **exact meaning**:

| State | `ready` | Meaning |
| --- | --- | --- |
| `ready` | true | Current published core snapshot; last collection succeeded. |
| `degraded` | true | Snapshot still current, but a collection attempt failed or an optional group of that sample is partial/error. |
| `starting` | false | No valid snapshot or first collection in progress; not yet failed/expired. |
| `stale` | false | No current core snapshot, due to expired last data or initial failure/slow collection. |

If the status state and `ready` field contradict one another, Hub must report a **protocol error**, not silently choose the favorable interpretation. Sample readings must never be treated as fresh solely because HTTP returned 200 or the last collection attempt succeeded.

`/api/stats` supplies `schema_version: 1`, `sample`, `collectors`, `sensor_meta`, and the data groups. In particular:

- `sample.instance_id` + `sample.sequence`: **publication identity** in a process. Repeated polls may return the same pair. `sequence` starts at 1 and increments only when a sample is published. It resets after an agent restart; compare sequences only **within the same instance**.
- `sample.started_at` and `completed_at` are **wall-clock timestamps**, useful for inspection but not reliable for age/order under clock adjustments. `sample.data_age_ms`, `age_ms`, `cycle_age_ms` and `stale_after_ms` use the agent's elapsed clock.
- `data_age_ms` is the age of the **oldest read represented** (or a documented conservative collection-start bound). It does not guarantee the underlying hardware refreshed at that instant.
- The agent considers data current while `data_age_ms <= stale_after_ms`; a collection longer than `stale_after_ms` is discarded rather than reported as a fresh sample.
- `collectors.core.state=ok` means required collection succeeded. An optional `collectors.*.state` can be `ok`, `partial`, `error` or `unavailable`. `unavailable` often means unsupported hardware or no data now, **not** an error. Partial/error means diagnostic degradation, not necessarily a stale core snapshot.
- Missing keys, `null`, and warmup are **not numeric zero**. CPU rows absent during warmup are not idle cores; distinguish `cpu_sampling.unavailable` reasons (e.g. `warmup`, `gap`, `counter_regressed`). GPU may be null on the Pi without making the node degraded.

### GPU and NPU additions (1.1.0)

Both accelerator objects can be absent/null, or present with null `usage`, `freq`
and `max_freq`. A driver with no supported load source remains visible by `driver`
and `provider`; `unavailable/not_exposed` differs from `not_detected`. Usage is
percent, clocks are Hz, and a measured zero is valid (including a gated clock).
Hub shows the source metadata and persists validated GPU/NPU percentages; Intel
GPU RC6 usage is time outside idle, not per-engine occupancy. No NPU threshold or
hardware identity is inferred.

### Clock and freshness across nodes

Hub must not compare the Orin and Pi wall-clock timestamps to determine which node is fresher or sort publication sequence across them. Store the Hub's receipt time (UTC for audit) plus a **Hub monotonic receipt tick**; compute elapsed time locally since receipt. For a cached response, display `agent_reported_data_age_ms + hub_elapsed_since_receipt_ms` as an **estimated lower bound** (network transit time is not included), and stop labeling a cached sample current when this exceeds `stale_after_ms` plus the Hub core poll interval and request deadlines (see [state semantics](architecture.md#default-polling-and-state-semantics); upstream `stale_after_ms` is only `max(3 × interval, 5 s)`, about one poll period). Add an explicit Hub poll/connection error and last-success age if contact is lost.

A newer Hub poll returning the **same** `(node_id, instance_id, sequence)` must only refresh reachability/age; it must **not** append a duplicate published measurement. On a changed `instance_id`, start a new sample generation even if its sequence is smaller. If the same instance returns an older sequence, reject it from the latest sample rather than overwriting newer data. Malformed timing values must not produce an optimistic Live assessment.

## 3. Stable identities

Hub's `node_id` is durable and assigned in its registry independently of platmon's protocol. The registry requires a canonical UUID. It is never derived from an IP address, hostname, Linux machine ID, serial number or agent `instance_id` (none is guaranteed to be a stable global ID by the platmon API). Endpoint/IP/hostname can change without a new logical node.

| Entity | Identity key | Lifetime / caution |
| --- | --- | --- |
| Managed node | **Hub `node_id`** | Persist through Hub and agent restarts; manually resolve hardware replacement. |
| Agent process | `(node_id, instance_id)` | New instance on agent restart, never automatically treated as a different managed node. |
| Core published sample | `(node_id, instance_id, sample.sequence)` | Deduplicate identical responses; monotonic sequence only within one instance. |
| Low-frequency observation | `(node_id, instance_id, group_name, observation.id)` | Separate publication counters per group, not core sequence. |
| Sensor metadata source | `(node_id, source_id)` plus process/generation context when charting | Source ID represents an attributed source/path, **not** a serial or replacement-proof identity; no cross-node deduplication. |
| History series | `(node_id, instance_id, series_id)` | May change after device/path/interface renumbering; don't join discontinuities. |

`source_id` in `sensor_meta` is an internal source descriptor hash, not a guarantee of same physical sensor forever. JSON Pointers in `sensor_meta` address **positions within one snapshot** (labels and rows can change); do not use them as persistent fleet-wide IDs.

## 4. Independent low-frequency observations

`/api/observations` uses **schema 2** in platmon v1.2.0. It includes `instance_id`, `clock`, and `groups` containing zero or more of `storage`, `wifi`, `probe`, and `battery`, each with its own publication `observation.id`, times and `interval_ms`/`stale_after_ms`.

- The `groups` map is legitimately empty if no groups are enabled. A group may be absent or idle by configuration; that is not an API violation.
- The group's `state` may be `starting`, `stale`, `ok`, `partial`, `error`, or `unavailable`. A stale group may **still contain old `data`**, which must be visibly noncurrent.
- Group `observation.data_age_ms` measures its own age on the agent elapsed clock; `stale_after_ms` is three group intervals. Compare those together (or use the supplied `stale`) independently of the core snapshot.
- For Wi-Fi, schema 2's `connected` is **true / false / null**. `null` means **unknown**, not disconnected. The additive `connection_state` distinguishes `connected`, `disconnected`, `unknown`, `unsupported`, `permission_denied`, `unavailable`.
- The configured `probe` observes a **TCP connection handshake** to an explicitly configured IP:port, not an ICMP ping and not end-to-end application responsiveness. Hub polling does **not** configure or trigger probes, and probe failures must not be interpreted as the agent being offline.
- Network counters belong to the agent's own network namespace. Do not sum interface rates into a made-up host throughput or infer an uplink interface without a separate contract. Disk I/O `io_time_ratio` is time with I/O in flight, not necessarily saturation.

A failed `/api/observations` request is an **optional endpoint error**; 404 and unsupported schemas are marked **unsupported/incompatible** without raising the optional-error alert while a correctly read core sample remains usable. Versions of an older agent that answer 404 on this optional endpoint should not become transport `offline`. `/api/history` 404 means disabled; it also must not become transport `offline`.

### Battery addition (1.2.0)

The optional `battery` group follows the same publication identity and freshness
rules as other observations. Its default interval is 60 seconds and stale threshold
is three intervals. Hub's existing ten-second observations polling only reads the
cache; it does not trigger battery collection. A missing group or
`unavailable/not_detected` is normal on devices without a battery.

Hub displays and persists selected `batteries` and `external` fields from the
[battery contract](https://github.com/platmon/platmon/blob/v1.2.0/docs/battery.md).
It preserves `present`/`online` as true/false/null; an empty battery bay has no
readings. Charge (µAh) and energy (µWh) are never converted into one another.
Voltage is µV, current is signed µA, power is signed µW; driver status determines
charging/discharging. Full/design ratios compare values within one family only.
The UI converts display units to Ah, Wh, V, A and W; observation exports retain
the original units. No charge control, low-charge alert or battery graph is added.

## 5. Parsing / version policy

| Condition | Hub policy |
| --- | --- |
| Expected major endpoint schema, required fields valid | Parse supported fields; ignore unknown **additive** JSON keys. |
| Missing `schema_version`, wrong type or unsupported integer for a **required** endpoint | `incompatible`; do not pretend legacy status is current. |
| Unknown `/api/observations` version or 404 | Mark only that optional feature unsupported/degraded; keep healthy core status if otherwise valid. |
| Valid status `ready=false` | Display agent `starting` or `stale`; no attempt to force new collection or fake sample values. |
| Stats 503 after `ready=true` | Race/no current data; show stale/no current sample, retry next scheduled cycle; not offline. |
| Invalid JSON, truncated content, missing mandatory field, incompatible status/ready pair | Explicit protocol error; leave last good values labeled last known. |
| Socket/DNS/connect/read timeout | Transport failure; apply Hub's two-consecutive-failure offline policy, subject to the [worker and DNS limits](architecture.md#default-polling-and-state-semantics). |
| Sample with `null`/unsupported optional metrics | Preserve unavailable semantics, never substitute zero. |

All JSON must be decoded with size bounds and type/range validation for fields used to make health or freshness decisions. Schema support is **per endpoint**, not one global Hub-compatible version. When a newer agent introduces an incompatible schema version, Hub must fail visibly for the affected required endpoint rather than guessing.

## 6. Agent history boundary and Hub retention

Agent `/api/history` stores only recent, capped memory series (default retention **600 seconds**, configurable 60–3600) and resets on service restart. The endpoint may be disabled. Values are indexed by `series_id` and publication ID with **age relative to the response**, not trustworthy global absolute sample timestamps. A `null` means missing/unknown or a discontinuity; never interpolate across gaps or convert it to zero. Independent slow groups have their own publication IDs; `series_intervals_ms` provides their cadence.

Hub does **not** scrape `/api/history` to backfill its database. Optional SQLite persistence stores accepted core and independent observations with Hub receipt time, bounded retention, and explicit continuity/gap evidence. See [history schema](history-schema.md) and [ADR-0002](adr/0002-hub-history-storage.md). Stored History and reports cannot reconstruct unobserved or expired intervals from the Agent memory buffer.

## 7. Source anchors and change control

- [v1.2.0 API fields and collector states](https://github.com/platmon/platmon/blob/v1.2.0/docs/api.md)
- [v1.2.0 independent observations and schema-2 Wi-Fi](https://github.com/platmon/platmon/blob/v1.2.0/docs/observations.md)
- [v1.2.0 history and gap semantics](https://github.com/platmon/platmon/blob/v1.2.0/docs/history.md)
- [v1.2.0 passive network scope](https://github.com/platmon/platmon/blob/v1.2.0/docs/network.md)
- [v1.2.0 TCP probe meaning](https://github.com/platmon/platmon/blob/v1.2.0/docs/probe.md)
- [v1.2.0 battery and external power](https://github.com/platmon/platmon/blob/v1.2.0/docs/battery.md)

Synthetic compatibility coverage lives in [test_agent_v120.py](../../tests/test_agent_v120.py)
and [the pinned payload fixture](../../tests/fixtures/agent-v1.2.0.json); these are
contract tests, not evidence of a new physical-device acceptance run.

Future changes to Hub's accepted schema set need fixtures and a compatibility note; do not silently upgrade requirements on the released agent. The implemented Hub state mapping and polling defaults are documented in [architecture.md](architecture.md), **not** promised behaviors of platmon.
