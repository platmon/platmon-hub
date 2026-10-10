# Backend behavior

This is a **read-only, explicitly registered Fleet backend and Dashboard** for platmon agents, with optional SQLite history, events and internal alerts. Local registry administration is separate from the HTTP read API. It never installs or controls Agents.

## Setup

Requires Python **3.9+** on a Linux or Windows Hub host with direct, operator-approved reachability to the Linux agents' TCP port (9797 by default). Runtime uses the Python standard library only.

1. Copy `nodes.example.json` to a private `nodes.json` outside the source checkout.
2. Create canonical opaque UUIDs with `python3 -c 'import uuid; print(uuid.uuid4())'`; keep them stable through IP changes and agent restarts.
3. Add explicit entries. Example using documentation IP addresses (replace them with real *approved* addresses):

   ```json
   {
     "nodes": [
       {"node_id": "00000000-0000-0000-0000-000000000001", "name": "Orin", "endpoint": "http://192.0.2.1:9797", "enabled": true},
       {"node_id": "00000000-0000-0000-0000-000000000002", "name": "Pi", "endpoint": "http://192.0.2.2:9797", "enabled": true}
     ]
   }
   ```

4. Start locally: `python3 -m hub --nodes /path/to/nodes.json`. The read-only HTTP server binds to **127.0.0.1:9787** by default. Valid registry edits reload automatically after a short debounce. To bind non-loopback, place the Hub behind an operator-controlled authenticated and encrypted access layer; the Hub itself has **no built-in authentication or TLS**; prefer keeping its listener on loopback behind the proxy.
5. Open `http://127.0.0.1:9787/` in a local browser or inspect `curl -fsS http://127.0.0.1:9787/api/nodes`. The browser fetches only Hub endpoints, never agents directly.

The Hub listens on **TCP 9787** by default (Agent listens on **TCP 9797**). Use `--port 9798` to select a different fixed port if needed; no automatic port selection or fallback is performed. Startup reports an actionable error if the listener cannot bind, including port-in-use and permission/reservation failures. See [port policy and conflict diagnosis](docs/ports.md) for Windows and Linux commands.

No automatic network scanning, browser-supplied agent URLs, writable registry API, outbound Push, or agent control is implemented. No agent-side modifications are required.

## Read-only Fleet Dashboard

The zero-dependency static page at `/` shows a fleet-wide count of Live, Partial, Stale/Starting and Offline/Error nodes, node cards (CPU, GPU, memory, root filesystem and hottest sensor), and a selected-node detail including per-core CPU, temperatures, optional observations and per-interface network/disk I/O rates when present. Unsupported and missing readings are marked N/A, **never zero**. Cached metrics remain visible as **last known** with their age once `data_current` is false.

The browser polls Hub `/api/nodes` every 5 s while visible and `/api/nodes/<node_id>` for the selected node. It does **not** trigger additional agent reads, discover endpoints or fetch arbitrary URLs. Hidden tabs stop requesting data until visible again; failed Hub requests show a connection banner without silently turning last-known values current. All agent-provided labels are rendered using DOM `textContent`, never interpolated as HTML. Assets are served from an exact allowlist with a restrictive Content Security Policy; there are no external browser dependencies.

The Hub's `/api/nodes` summary adds an `overview` object with optional `cpu_percent`, `gpu_percent`, `memory_percent`, `disk_percent`, and `hottest_c` fields; unavailable readings are JSON null. This is an additive, Hub-owned schema-1 projection. It is computed only from the last accepted agent snapshot; callers must consult `data_current` and `headline` before calling the values live. The detail response includes the same `overview` and original cached `stats` / independent `observations`.

History and internal alerts are opt-in. Authenticated remote viewing requires the separately configured proxy; see [security](docs/security.md). Synthetic capacity and actual three-device evidence have explicit [limits](docs/resource-budget.md). Default bind remains loopback.

## Hub endpoints (Hub schema 1)

| Route | Response |
| --- | --- |
| `GET /healthz` | Hub server process responsiveness only; does **not** imply agent health. |
| `GET /api/nodes` | All registered nodes and status/age diagnostics, no full metric payloads. |
| `GET /api/nodes/<node_id>` | Single node with last-known agent stats/observations and explicit `data_current`. |

Writes return HTTP 405. Unknown resources return 404. All supported responses are JSON with `Cache-Control: no-store`; there is no arbitrary URL-fetch/proxy endpoint. The hub's own API schema is **independent** of the agent's endpoint schemas.

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

**Runtime limitation:** `urllib` socket timeouts do not forcibly cancel a blocked operating-system DNS resolution. Poll work stays bounded to 16 workers; shutdown may block waiting on such a resolver. True end-to-end hard deadlines or process isolation require separate implementation/measurement before untrusted large-fleet deployment. The scheduler applies bounded exponential backoff to failed core transport polls, bounded retry delay to optional failures, and up to 0.5 s completion-time jitter. No polls overlap per node; precise cadences are not guaranteed under saturation. Actual three-device read-only collection and restart checks passed on Linux and Windows; see [measured limits](docs/resource-budget.md). These are not production capacity guarantees.

## Development validation

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q hub tests
```

Fixtures use synthetic schema-1/status/stats and schema-2/observations payloads, HTTP test servers and a monotonic fake clock. These are not a substitute for the [hardware smoke plan](docs/mvp-acceptance.md) and are distinct from the short three-device field checks documented in the resource budget.

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
See [the storage contract](docs/adr/0002-hub-history-storage.md).

### Observations and discontinuities (introduced in history schema 2)

Storage, Wi-Fi and TCP-probe publications are deduplicated independently by node,
instance, group and observation ID. Only fresh, actually received publications are
stored; unavailable values stay null. The selected payload is limited to 16 KiB:
filesystem device numbers and byte capacities; Wi-Fi interface identity, tri-state
connection and signal/quality values in their original units; probe target,
handshake milliseconds and attempt/failure statistics. Unknown fields are omitted.
No probe is configured or triggered by Hub. Core sequence is never an optional ID.

Schema 1 upgrades transactionally to schema 2 after a verified adjacent
`.v1-backup-<unique-id>` snapshot. Keep that backup for rollback with Hub stopped;
older software cannot open newer schemas. The current schema is 4; see [migration history](docs/history-schema.md). New tables record group publications,
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
allowlist: `cpu_percent`, `gpu_percent`, `memory_percent`, `disk_percent`,
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

Device details now include a separate History panel for CPU, memory, GPU, highest
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

Add generates a UUID; all other changes retain it. `add --disabled` registers an
inactive target. Remove only removes the registry entry; it never deletes history.
All commands preserve existing limits and endpoint validation and never contact
an agent. Writes take a nonblocking adjacent process lock, validate the entire
result, save the previous bytes as `nodes.json.bak`, fsync a temporary file, then
atomically replace the registry. Invalid input or replace failure leaves the
original intact. Cooperative CLI writers cannot overwrite each other; manual
editors must coordinate with the operator. The latest backup is a recovery copy,
not an indefinite audit log. Protect the registry, backup and directory with OS
permissions/ACLs; no web management API is provided. The running Hub watches this file by default; `--no-registry-reload` retains startup-only behavior.


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
[alert evaluation and restoration policy](docs/alerts.md).

## Access boundary

HTTP remains read-only. Local loopback requests must use Host `localhost`,
`127.0.0.1` or `::1`; a proxy must rewrite its upstream Host accordingly.
Admission limits are 32 connections and a shared 100 GET/s budget (burst 200),
with 5-second socket inactivity timeout. Oversized targets/headers are rejected.
See [security and optional TLS authentication](docs/security.md) for roles,
credentials, audit limitations and the opt-in nginx example.
