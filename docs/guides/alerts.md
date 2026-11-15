# Internal alerts

[Documentation index](../README.md) · [User guide](README.md)

Enable with `--history-db /path/history.db --alerts /path/alerts.json`.
Review `alerts.example.json` against each device's safe operating temperatures;
its thresholds are examples, not hardware safety limits. Rules apply to all enabled
nodes. Alerts stay in Hub unless the configuration has a [webhook](#webhook); there is
no credential storage and no management HTTP write.
Edit the local configuration and restart the Hub to change rules. Registry hot reload
remains independent. Configuration is at most 64 KiB, with at most 32 unique rules.

Numeric conditions: `cpu_percent`, `memory_percent`, `disk_percent`, `hottest_c`,
`gpu_percent`, `npu_percent`, `probe_failure_percent`, `psi_cpu_some_avg10`,
`psi_memory_some_avg10`, `psi_io_some_avg10`. Require finite `warning <= critical`;
percentages are 0..100.

The `psi_*` conditions are the Agent's pressure stall information (`some avg10`): the
share of the last 10 seconds in which some tasks waited on CPU, memory or I/O. It is not
usage; a busy CPU with nothing waiting has no pressure. Pressure is part of the core
sample, so it follows core freshness. Agents without a pressure group, or kernels
without PSI (Jetson L4T and WSL kernels among them), give unknown evidence, never zero.
There is no example rule: useful levels depend on the workload.

`probe_failure_percent` is the worst probe target's share of failed TCP connect attempts
over the agent's last 10 attempts to that target, as a percentage. It follows the `probe`
group's own freshness, not core freshness, so it keeps being evaluated while core data
is stale. A stale or absent group, or one with no target, is unknown. One failure in
10 is 10%, so a warning below 10 fires on any single failure. It measures failed
connection attempts from the agent, not packet loss on the path.

Low conditions, where the low reading is the bad one: `battery_percent` (0..100, the
lowest charge of the present batteries) and `wifi_signal_dbm` (-120..0, the weakest
signal of the connected interfaces). They require `warning >= critical`. A value at or
below warning enters Pending, at or below critical is critical severity, and recovery
needs a value strictly above warning plus hysteresis, so `warning + hysteresis` must
not exceed the ceiling. Like the probe condition, they follow their own observation
group's freshness; an empty bay, an ended connection, or a stale or absent group is unknown.
Agent `[thresholds] battery` and `wifi` only color the Agent's own viewers; Hub rules
are configured here independently.
Boolean conditions: `offline`, `stale`, `optional_error` (no numeric thresholds).
`optional_error` ignores agents that do not provide `/api/observations` (404) or
report an unknown observations schema; those show as `unsupported` or `incompatible`.
Rule IDs use 1..48 lowercase letters, digits, hyphens or underscores.

Evaluation runs once per second outside polling workers. Durations use monotonic
time. Defaults: pending 30 seconds, recovery 30 seconds, cooldown 300 seconds,
hysteresis 5 units. A value at or above warning enters Pending. It must remain
above warning for `duration_seconds` to become Active. Critical severity escalates
immediately while Active and remains latched until resolution. A value strictly
below warning minus hysteresis enters Recovering; `recovery_seconds` of continuous
recovery evidence resolves it. Rebound cancels recovery. Resolved rules cannot
re-enter Pending before cooldown expires. Zero durations take effect on the next
evaluation tick. Threshold changes reset the rule on the next Hub startup.

Missing/unsupported/stale numeric data is unknown, never zero or a recovery.
Unknown evidence cancels Pending and cancels Recovering back to Active. Offline
and Stale conditions recover only with a current core sample. Optional errors
include transport/contract failures and fresh collector error/partial states;
unsupported groups are ignored. Optional failure does not change core status.
Removed/disabled nodes resolve with `node_inactive` and leave the active-state table.

Schema 4 adds `alert_states`: latest configuration/state per node/rule (at most
8,192 for the current registry/rule set). Orphaned states after dropped removals
can exceed this; restoration then pauses with `alert_restore_error` in writer
diagnostics while core/history collection continues. State and an `alert_transition` event commit
in the same writer transaction. Event retention is independent of current state.
On restart identical rules restore Active/Recovering as Active with unknown current
evidence. Pending restarts its full duration; Resolved restarts a full cooldown.
No elapsed downtime is interpreted as successful evaluation. Changed/removed rules
produce a resolution event and their old current state is deleted.

Storage remains best effort under failure. Queue admission is not commit success.
The engine retains at most 8,192 latest unsent states and exposes pending admission
and lost-transition counters; intermediate transitions can be coalesced while the
queue is full. Writer counters expose losses after admission. A crash/storage outage
can lose transitions or the newest state. Recovery uses the last committed state;
it cannot guarantee delivery or reconstruct unobserved transitions.

`GET /api/alerts` returns Hub schema 1, enabled/ready/error, at most 500 non-Normal
states (active first), total count and persistence diagnostics. Each row identifies
node/rule/condition, state/severity, evidence known/value and UTC changed_at_ms.
The dashboard pauses requests while hidden, uses bounded responses and displays
unknown evidence explicitly. This is an operator aid, not a safety interlock.

## Webhook

Off by default. An optional `webhook` object beside `rules` sends alert transitions to one
URL with an HTTP `POST` of a JSON document:

```json
{
  "rules": [{"id": "temperature-high", "condition": "hottest_c", "warning": 80, "critical": 90}],
  "webhook": {"url": "https://hooks.example.com/services/T000/B000/XXXX", "timeout_seconds": 5}
}
```

`url` is `http://` or `https://` with a host, at most 2048 characters, without a user name or
password; `timeout_seconds` is 1..30 (default 5). Restart Hub to change it, as for rules.

Three transitions leave Hub, one document each: `firing` (Pending to Active), `escalated`
(warning to critical while Active) and `resolved` (an Active or Recovering alert resolves,
also with reason `node_inactive` when its node is removed or disabled). Pending, Recovering,
a rebound from Recovering to Active, and the resolutions Hub writes for changed rules at
startup are not sent. A restored Active alert is not sent again after a Hub restart.

```json
{"schema_version": 1, "source": "platmon-hub", "event": "firing",
 "text": "platmon-hub: temperature-high firing on Bench board (warning, value 82.5)",
 "node_id": "...", "node_name": "Bench board", "rule_id": "temperature-high", "condition": "hottest_c",
 "previous_state": "Pending", "state": "Active", "severity": "warning", "reason": "condition",
 "known": true, "value": 82.5, "warning": 80, "critical": 90, "changed_at_ms": 1763164800000}
```

`text` is a one-line summary, the field a Slack incoming webhook displays; other services that
expect a different body need a small relay. `warning` and `critical` are null where the
thresholds are not known. Receivers should ignore unknown keys.

Delivery runs on its own thread from a queue of at most 256 transitions; when it is full the
oldest gives way and `dropped` counts it. Evaluation only adds to the queue, so a slow or
unreachable endpoint never delays polling, evaluation or History. Each transition gets up to
three attempts (after 1 and 5 seconds); any `2xx` answer is success, and anything else,
including a redirect, which is not followed, is a failure after the third attempt. Hub does not
use an HTTP proxy from the environment. The queue is in memory: transitions still queued when
Hub stops are lost, and Hub does not resend them. `GET /api/alerts` and the Alerts panel show
delivered, failed, dropped and queued counts and the last error (`timeout`, `connection`,
`http_<status>`); the log gets one line when the error changes. This is a best-effort
notification, not a guaranteed delivery or a safety interlock.

See [security](security.md#alert-webhook) for what leaves the host.
