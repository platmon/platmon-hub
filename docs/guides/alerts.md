# Internal alerts

[Documentation index](../README.md) · [User guide](README.md)

Enable with `--history-db /path/history.db --alerts /path/alerts.json`.
Review `alerts.example.json` against each device's safe operating temperatures;
its thresholds are examples, not hardware safety limits. Rules apply to all enabled
nodes. No external notifications, credential storage or management HTTP writes exist.
Edit the local configuration and restart the Hub to change rules. Registry hot reload
remains independent. Configuration is at most 64 KiB, with at most 32 unique rules.

Numeric conditions: `cpu_percent`, `memory_percent`, `disk_percent`, `hottest_c`,
`gpu_percent`. Require finite `warning <= critical`; percentages are 0..100.
Boolean conditions: `offline`, `stale`, `optional_error` (no numeric thresholds).
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
