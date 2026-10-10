# Backend foundation (PR #2)

This implementation is a **read-only, manually registered fleet backend** for platmon v1.0.0. It is not a web dashboard, an agent installer, alerting platform, or persistent metrics store.

## Setup

Requires Python **3.9+** on a Linux host with direct, operator-approved reachability to the agents' TCP port (9797 by default). Runtime uses the Python standard library only.

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

4. Start locally: `python3 -m hub --nodes /path/to/nodes.json`. The read-only HTTP server binds to **127.0.0.1:9787** by default. A modified registry requires a Hub restart. To bind non-loopback, place the Hub behind an operator-controlled authenticated and encrypted access layer; the initial MVP itself has **no authentication or TLS**.
5. Inspect `curl -fsS http://127.0.0.1:9787/api/nodes` or `GET /api/nodes/<node_id>`.

No automatic network scanning, browser-supplied agent URLs, writable registry API, outbound Push, or agent control is implemented. No agent-side modifications are required.

## Hub endpoints (provisional schema v1)

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

**Runtime limitation:** `urllib` socket timeouts do not forcibly cancel a blocked operating-system DNS resolution. Poll work stays bounded to 16 workers; shutdown may block waiting on such a resolver. True end-to-end hard deadlines or process isolation require separate implementation/measurement before untrusted large-fleet deployment. The scheduler applies bounded exponential backoff to failed core transport polls, bounded retry delay to optional failures, and up to 0.5 s completion-time jitter. No polls overlap per node; precise cadences are not guaranteed under saturation. Capacity and performance have **not** been validated on actual Orin/Pi hardware.

## Development validation

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q hub tests
```

Fixtures use synthetic schema-1/status/stats and schema-2/observations payloads, HTTP test servers and a monotonic fake clock. These are not a substitute for the [hardware smoke plan](mvp-acceptance.md) and do not claim actual board validation.
