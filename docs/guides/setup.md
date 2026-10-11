# Hub setup and backend behavior

[Documentation index](../README.md) · [User guide](README.md)

This is a **read-only, explicitly registered Fleet backend and Dashboard** for platmon agents, with optional SQLite history, events and internal alerts. Registry administration is available through the local CLI and opt-in local-only web management routes. It never installs or controls Agents.

## Setup

Follow the [Quick Start](quick-start.md) for an empty dashboard. Source-checkout commands below run from the **repository root**, containing `hub/` and `nodes.example.json`, even though this document lives in `docs/guides/`. An installed wheel provides `platmon-hub` in place of `python -m hub`; see [installation](operations.md).

## Register agents and start monitoring

Requires Python 3.9+ and direct reachability from the Hub host to already-running Linux agents (normally TCP 9797). Use an agent's approved IP/hostname; `127.0.0.1` in an endpoint refers to the Hub computer. Stop the empty Quick Start instance with Ctrl+C before starting another Hub on the same port.

Create a private state directory and copy the empty template only when the registry is absent. If you already have a registry, set the path to that existing file to retain its UUIDs. The interactive `add` command prompts for a name, host and port, shows a preview, and saves only after you type `yes`.

**Windows PowerShell** (substitute `py -3` for `python` if needed):

```powershell
$hubState = Join-Path $env:LOCALAPPDATA 'platmon-hub'
New-Item -ItemType Directory -Force -Path $hubState | Out-Null
$registryPath = Join-Path $hubState 'nodes.json'
if (-not (Test-Path -LiteralPath $registryPath)) {
  Copy-Item -LiteralPath .\nodes.example.json -Destination $registryPath
}
python -m hub nodes --file "$registryPath" add --interactive
python -m hub nodes --file "$registryPath" validate
python -m hub --nodes "$registryPath"
```

**Linux:**

```sh
mkdir -p "$HOME/.config/platmon-hub"
registry_path="$HOME/.config/platmon-hub/nodes.json"
test -e "$registry_path" || cp ./nodes.example.json "$registry_path"
python3 -m hub nodes --file "$registry_path" add --interactive
python3 -m hub nodes --file "$registry_path" validate
python3 -m hub --nodes "$registry_path"
```

Continue to startup only after validation succeeds. To reuse an already-populated registry without adding a device, skip `add --interactive`. The CLI generates stable UUIDs and preserves existing entries; it does not contact or install an agent.

Open [http://127.0.0.1:9787/](http://127.0.0.1:9787/) on the Hub computer. Registered devices should appear after polling; use their status/age diagnostics if they remain Starting, Offline, or Error. `/healthz` confirms only Hub responsiveness.

The registry path is explicit on each raw CLI start. Relative paths resolve from the current working directory; quote paths containing spaces. Use the **same file** for the Hub's `--nodes` and the registry CLI's `--file`. Valid changes reload automatically by default; `--no-registry-reload` requires a restart. Renames and endpoint changes preserve UUIDs.

For all fields/options, see [configuration](configuration.md). For guided, noninteractive and update commands, see [local registry CLI](#local-registry-cli); the [Devices tab](web-registration.md) requires opt-in local web administration. First-run problems are covered in [troubleshooting](troubleshooting.md).

The Hub defaults to TCP 9787 and agents to TCP 9797. `--port 9798` changes the Hub URL to `http://127.0.0.1:9798/`; it does not change agent ports. There is no automatic fallback. Use [port diagnostics](ports.md) for bind failures.

## Read-only Fleet Dashboard

The zero-dependency static page at `/` shows a fleet-wide count of Live, Partial, Stale/Starting and Offline/Error nodes, node cards (CPU, GPU, NPU, memory, root filesystem and hottest sensor), and a selected-node detail including per-core CPU, temperatures, optional observations and per-interface network/disk I/O rates when present. Unsupported and missing readings are marked N/A, **never zero**. Cached metrics remain visible as **last known** with their age once `data_current` is false.

The browser polls Hub `/api/nodes` every 5 s while visible and `/api/nodes/<node_id>` for the selected node. It does **not** trigger additional agent reads, discover endpoints or fetch arbitrary URLs. Hidden tabs stop requesting data until visible again; failed Hub requests show a connection banner without silently turning last-known values current. All agent-provided labels are rendered using DOM `textContent`, never interpolated as HTML. Assets are served from an exact allowlist with a restrictive Content Security Policy; there are no external browser dependencies.

The Hub's `/api/nodes` summary adds an `overview` object with optional `cpu_percent`, `gpu_percent`, `npu_percent`, `memory_percent`, `disk_percent`, and `hottest_c` fields; unavailable readings are JSON null. This is an additive, Hub-owned schema-1 projection. It is computed only from the last accepted agent snapshot; callers must consult `data_current` and `headline` before calling the values live. The detail response includes the same `overview` and original cached `stats` / independent `observations`.

History and internal alerts are opt-in. Authenticated remote viewing requires the separately configured proxy; see [security](security.md). Synthetic capacity and actual three-device evidence have explicit [limits](../development/resource-budget.md). Default bind remains loopback.

## Hub endpoints (Hub schema 1)

| Route | Response |
| --- | --- |
| `GET /healthz` | Hub server process responsiveness only; does **not** imply agent health. |
| `GET /api/nodes` | All registered nodes and status/age diagnostics, no full metric payloads. |
| `GET /api/nodes/<node_id>` | Single node with last-known agent stats/observations and explicit `data_current`. |

Writes return HTTP 405 by default; the opt-in local management routes are documented in [web management](web-registration.md). Unknown resources return 404. All supported responses are JSON with `Cache-Control: no-store`; there is no arbitrary URL-fetch/proxy endpoint. The hub's own API schema is **independent** of the agent's endpoint schemas.

## Agent consumption

The Hub calls only three fixed GET paths: `/api/status` (schema 1), `/api/stats` (schema 1 when ready) and `/api/observations` (schema 2 on an independent cadence). `/api/history` is **not** polled. The cache is ephemeral: restart loses last-known measurements; the **explicit registry file** persists `node_id` and endpoints.

| Setting | Initial value |
| --- | --- |
| Core polling | 5 s after previous completed core poll |
| Observation polling | 10 s after previous completed observation poll (best-effort) |
| Status / stats / observations I/O timeout | 2 / 2 / 3 s (socket timeout, not strict DNS deadline) |
| Concurrent node operations | At most 16; one in-flight operation per node |
| Maximum agent response body | 512 KiB per request |
| Offline rule | Two consecutive core transport failures; a valid status resets count |
| Freshness | Agent `data_age_ms` + Hub monotonic elapsed; poll/deadline grace only for cached replies |

The scheduler rotates nodes fairly at capacity; time spent on a failed request does not block polling workers for other nodes while capacity is available. A success of `/api/status` followed by a `/api/stats` timeout is **not** treated as two consecutive unreachable cycles. Optional endpoint failures are reported separately from core health. Unknown schemas fail explicitly for required endpoints; unsupported `observations` never cause core Offline.

**Runtime limitation:** `urllib` socket timeouts do not forcibly cancel a blocked operating-system DNS resolution. Poll work stays bounded to 16 workers; shutdown may block waiting on such a resolver. True end-to-end hard deadlines or process isolation require separate implementation/measurement before untrusted large-fleet deployment. The scheduler applies bounded exponential backoff to failed core transport polls, bounded retry delay to optional failures, and up to 0.5 s completion-time jitter. No polls overlap per node; precise cadences are not guaranteed under saturation. Actual three-device read-only collection and restart checks passed on Linux and Windows; see [measured limits](../development/resource-budget.md). These are not production capacity guarantees.

## Development validation

Local checks, package validation, CI, and browser test tooling are documented in the [development guide](../development/README.md).

## Optional core history

Pass `--history-db /path/to/history.db` to enable SQLite persistence. The parent
must already exist on a local disk with write permission; otherwise live polling
continues and history reports its failure. `--history-retention-days` defaults to
7. History is disabled without an explicit path. Core receipt times are UTC
milliseconds; missing metrics remain null. Sample identity includes the Hub UUID,
agent instance and sequence. Duplicate or regressing sequences do not add rows.

A single background writer owns SQLite and its adjacent process lock; queueing is
nonblocking (2,048 records). Overflow and storage failures can lose history while
live monitoring continues. Read `/api/history/status` for queue, commit, drop and
error diagnostics; these counters reset with Hub. Shutdown attempts a five-second
writer drain; queued/uncommitted data is not crash-durable. Do not share a DB
between Hub processes or place it on a network share. Back up with SQLite's backup
API or after clean shutdown, not by copying an active DB without its WAL.
See [the storage contract](../design/adr/0002-hub-history-storage.md).

### Observations and discontinuities (introduced in history schema 2)

Storage, Wi-Fi, TCP-probe and battery publications are deduplicated independently by node,
instance, group and observation ID. Only fresh, actually received publications are
stored; unavailable values stay null. The selected payload is limited to 16 KiB:
filesystem device numbers and byte capacities; Wi-Fi interface identity, tri-state
connection and signal/quality values in their original units; probe target,
handshake milliseconds and attempt/failure statistics; battery charge/energy,
voltage, signed current/power, cycle count and external power state. Unknown fields are omitted.
No probe is configured or triggered by Hub. Core sequence is never an optional ID.

For platmon 1.2.0, device details include **Power & battery**, with separate battery
freshness and `Current` / `Last-known` labeling, power rails and fans. GPU/NPU
details show driver/provider and clocks, even when load is unavailable. Older
agents remain supported without these additional fields. Battery has observation
exports but no graph or charge-control action; NPU usage has a History graph.

Schema 1 upgrades transactionally to schema 2 after a verified adjacent
`.v1-backup-<unique-id>` snapshot. Keep that backup for rollback with Hub stopped;
older software cannot open newer schemas. The current schema is 5; see [migration history](../design/history-schema.md). New tables record group publications,
continuity evidence, gaps and Hub sessions. Core and optional history share the
configured retention period (default seven days). Diagnostic persisted/submitted
counts include continuity records as well as metric records.

Gaps distinguish observed transport failure, Offline, stale data, group absence,
instance changes and storage loss. Restarted Hub marks unobserved time without
asserting network failure. Evidence boundaries are approximate. Storage-loss
summaries are memory-bounded and persisted on recovery; crash during DB failure
can lose the reason, leaving an unobserved gap. Repeated Offline polls extend one
open gap, not new synthetic samples. A group failure never changes Core health.

### History read API

`GET /api/nodes/{node_id}/history?metric=cpu_percent&from_ms=0&to_ms=3600000&max_points=1200`
queries Hub receipt UTC milliseconds in the half-open range `[from_ms,to_ms)`.
Use actual epoch milliseconds for an operating installation. Metrics are a fixed
allowlist: `cpu_percent`, `gpu_percent`, `npu_percent`, `memory_percent`, `disk_percent`,
`hottest_c`. The maximum span is seven days and the maximum point count is 1,200.
Unknown/duplicate parameters, invalid bounds and metrics return 400; unregistered
nodes return 404; disabled/unavailable history returns 503.

Schema 1 returns `node_id`, metric/range, `points` and approximate `gaps`. Points
carry `timestamp_ms`, `instance_id`, `segment`, `average`, `minimum`, `maximum`
and the count of non-null samples. Nulls, generation changes, observed gaps and
silence above 15 seconds break segments. All-null buckets have count 0 and null
aggregates. Average is accompanied by extrema so spikes remain visible. The
first receipt in each bucket supplies its timestamp. If preserving boundaries
would exceed the requested point budget, 422 `history_range_too_dense` asks the
caller to narrow the time range. Queries stream over a separate read connection
with a two-second deadline and bounded result memory; they never trigger polls.
Query parameters remain forbidden on static assets and other existing routes.

### Browser history

Device details now include a separate History panel for CPU, memory, GPU, NPU, highest
temperature and root-disk usage. Select a metric and 1h/6h/24h/7d range. Canvas
renders average lines and min/max bars, approximate shaded gaps and dashed agent
restart boundaries. Missing values are N/A, never zero. Hover or use arrow keys
on the focused chart for UTC time, extrema, count and agent instance.

Only the selected node/metric/range is retained in browser memory. History refreshes
at most every 30 seconds during the existing visible-tab refresh cycle; switching
selection cancels the old request. Hidden tabs cancel and pause History. Query
failures stay within this panel. No CDN, external font or chart library is used.

Optional browser verification: with Playwright available in the development
Node module path and an empty Hub listening on a loopback test port, run
`node scripts/check-history-ui.cjs http://127.0.0.1:PORT`. It supplies synthetic
API responses and checks nulls, selection cancellation, hidden-tab pause, failure
isolation and light/dark mobile layout. Playwright is verification tooling only,
not a Hub dependency. Screenshots are written to the OS temporary directory.

## Local registry CLI

Manage the same explicit registry without editing JSON by hand:

```sh
python -m hub nodes --file /path/to/nodes.json list
python -m hub nodes --file /path/to/nodes.json validate
python -m hub nodes --file /path/to/nodes.json add --name "Lab node" --endpoint http://192.0.2.1:9797
python -m hub nodes --file /path/to/nodes.json rename NODE_UUID "New name"
python -m hub nodes --file /path/to/nodes.json set-endpoint NODE_UUID http://192.0.2.2:9797
python -m hub nodes --file /path/to/nodes.json disable NODE_UUID
python -m hub nodes --file /path/to/nodes.json enable NODE_UUID
python -m hub nodes --file /path/to/nodes.json remove NODE_UUID
```

For a new device, the shortest noninteractive command is:

```sh
python -m hub nodes --file /path/to/nodes.json add --name "Lab node" --host 192.0.2.1
```

`--host` accepts an explicit IP or hostname and uses Agent port `9797` by default.
Use `--port 12345` for another Agent port (not the Hub port). Bare or bracketed
IPv6 is accepted, for example `--host ::1`. Use `--endpoint` instead for a complete
plain HTTP origin; the existing command remains supported. Do not combine
`--endpoint` with `--host` or `--port`. No DNS lookup or connectivity check happens
at registration time; an unreachable approved target can still be registered.

For guided local registration:

```sh
python -m hub nodes --file /path/to/nodes.json add --interactive
```

Enter the display name, Agent IP/hostname and port (Enter accepts `9797`), review
the validated preview, then type `yes` to save. The confirmation defaults to no;
canceling or interrupted input writes nothing and does not acquire the registry
lock. Supplied `--name`, `--host`, `--endpoint` or `--port` skip those questions.
`--disabled` registers an inactive target. Prompts/preview use stderr and the
successful result remains one JSON object on stdout. The destination parent
directory must already exist. Registration never contacts or modifies an Agent.

On success the result contains the new `node_id`. The running Hub normally
applies the same registry automatically within about one second. Confirm it in
the Fleet Dashboard; inspect `/api/registry/status` for rejected changes. The
Hub's `--nodes` path and the CLI's `--file` path must refer to the same file.
With `--no-registry-reload`, restart the Hub to apply changes. Existing endpoints
are rejected with guidance to list and update the original UUID rather than
creating a second identity.

Add generates a UUID; all other changes retain it. `add --disabled` registers an
inactive target. Remove only removes the registry entry; it never deletes history.
All commands preserve existing limits and endpoint validation and never contact
an agent. Writes take a nonblocking adjacent process lock, validate the entire
result, save the previous bytes as `nodes.json.bak`, fsync a temporary file, then
atomically replace the registry. Invalid input or replace failure leaves the
original intact. Cooperative CLI writers cannot overwrite each other; manual
editors must coordinate with the operator. The latest backup is a recovery copy,
not an indefinite audit log. Protect the registry, backup and directory with OS
permissions/ACLs; [web management](web-registration.md) requires explicit `--web-admin` configuration. The running Hub watches this file by default; `--no-registry-reload` retains startup-only behavior.

## Registry hot reload

A dedicated watcher checks the local file every 250 ms and waits for 500 ms of
stable content before full validation. Reads are capped at 1 MiB. Missing,
malformed, duplicate or otherwise invalid registries leave the last valid fleet
running; `/api/registry/status` exposes the error category and reload count.
Returning to the previous valid file clears the error. No network discovery is
performed. The watcher never holds the registry lock while reading disk.

Applying a valid file is atomic with respect to scheduler and API readers. Names
change without clearing samples. Endpoint or enabled-state changes keep the UUID
and stored History but reset the live cache. Removed nodes receive no new polls.
An old in-flight request may finish within existing transport limits, but its
response is discarded if its state generation has been replaced. A re-added
node with the same UUID waits for that old request to finish: it cannot overlap
another request for the same node. Other nodes continue independently. The
existing OS DNS timeout limitation still applies. The watcher stops on shutdown;
no Hub or agent restart is needed to apply registry changes.

## Events (history schema 3)

With History enabled, Hub records transitions rather than repeated state polls:
core headline changes (including error/recovery), agent instance changes,
optional error/recovery by group, registry changes, writer failure/recovery and
queue overflow. Events carry UUID identity, optional node ID, Hub UTC receipt
milliseconds, previous/current state and a reason code. Queue-overflow event
notifications are coalesced to at most one per minute; drop counters retain the
actual count. Writer error events are best effort during an unavailable DB and
may be lost with the process before recovery. No alert is sent externally.

`GET /api/events` defaults to the past 24 hours. Optional `from_ms`, `to_ms`,
`node_id`, `type`, `limit` and `cursor` filter the result. Time ranges are half-open,
limited to 30 days; limit defaults to 100 and cannot exceed 500. The schema-1
response contains `events` and `next_cursor`; reuse the same range/filters with
the returned cursor to page backward without duplicate equal-timestamp entries.
Malformed filters return 400; disabled/unavailable persistence returns 503.
Removed-node events remain queryable by UUID. All routes are GET-only.

Event retention defaults to 30 days and is configurable with
`--event-retention-days`; it is independent of core/optional retention. Schema 2
upgrades to 3 with a verified backup and transactional migration. Repeated status
updates, gap changes and their derived events share the writer transaction.

## Alerts

`--alerts FILE` enables local rules when `--history-db` is also configured.
`GET /api/alerts` returns bounded, read-only alert state with independent Hub
schema 1. `alert_transition` is available through the Events API. SQLite schema 4
stores current alert configuration/state atomically with transitions. See
[alert evaluation and restoration policy](alerts.md).

## Access boundary

HTTP remains read-only by default. Local loopback requests must use Host `localhost`,
`127.0.0.1` or `::1`; a proxy must rewrite its upstream Host accordingly.
Admission limits are 32 connections and a shared 100 GET/s budget (burst 200),
with 5-second socket inactivity timeout. Oversized targets/headers are rejected.
See [security and optional TLS authentication](security.md) for roles,
credentials, audit limitations and the opt-in nginx example.
