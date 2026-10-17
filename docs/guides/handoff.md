# Downstream handoff

[User guide](README.md) · [Configuration](configuration.md) · [Full-publication replay](replay.md) · [ADR-0004](../design/adr/0004-downstream-data-handoff.md)

Handoff delivers what Hub stored to a separate post-processing system as files: immutable,
chained, compressed **segments** in a spool directory. Hub hands off observed facts only (raw
publications, replay losses, gaps, events, Hub sessions and its own handoff losses). Labels,
features, anonymization, long-term storage and models belong to the consumer. Hub never waits
for the consumer and never keeps data longer than its retention for it.

## Enable it

Handoff requires raw capture. Create the spool directory first; Hub does not create it.

```sh
python3 -m hub --nodes /private/nodes.json --history-db /private/history.db \
  --raw-capture --raw-retention-days 2 --raw-quota-mib 2048 \
  --handoff-spool /private/handoff --handoff-spool-quota-mib 4096 --handoff-spool-retention-days 1
```

| Option | Meaning |
| --- | --- |
| `--handoff-spool PATH` | Existing, writable directory on a filesystem with hard links |
| `--handoff-spool-quota-mib MIB` | Segment production pauses while the spool holds this much |
| `--handoff-spool-retention-days DAYS` | How long acknowledged segments stay; at most `--raw-retention-days` |
| `--handoff-start now\|existing` | First enable only: `now` excludes rows already stored (one `not_handed_off` loss per table); `existing` hands them off by backfill |

Startup refuses a missing, unwritable or hard-link-less spool and a spool retention longer than
the raw retention. Choose the raw retention as the longest consumer outage you tolerate: with a
consumer running, Hub raw storage is only a buffer (see [ADR-0004 storage levels](../design/adr/0004-downstream-data-handoff.md#storage-levels)).

Segments are built every 15 minutes, back to back while a backlog exceeds the 256 MiB segment
limit, and once more on a clean shutdown. Without a running Hub, the offline command
`python3 -m hub handoff --db DB status|enable --start now|existing|disable|build --spool DIR`
does the same with exclusive access to the database.

## Spool layout

```
<spool>/<store id>/00000000000000000001-00000000000000000806.zip
<spool>/<store id>/00000000000000000807-00000000000000000839.zip
<spool>/<store id>/acks/00000000000000000001-00000000000000000806.zip.ack
```

The store ID is random per history database; a restored backup or a second Hub has its own
chain. Each ZIP holds `raw_publications.jsonl`, `raw_envelopes.jsonl`, `replay_losses.jsonl`,
`gaps.jsonl`, `events.jsonl`, `hub_sessions.jsonl`, `handoff_losses.jsonl`, `nodes.jsonl` and
`manifest.json`. The manifest names the previous segment and its SHA-256 and lists every
member's row count, size and SHA-256. Raw rows carry decoded `data` and an `envelope_digest`
that refers to `raw_envelopes.jsonl`. Segments contain hostnames, sensor labels and probe
addresses: protect the spool like the history database.

## Consumer obligations

A consumer must:

1. Process segments in name order, starting from the first one it has not applied.
2. Check `format` = `platmon-hub-handoff` and `format_version` = 1, that `from_seq` and
   `previous_sha256` continue the last applied segment, and every member's SHA-256.
3. Apply rows by key, keeping the row with the larger `handoff_seq`: raw publications by
   (`node_id`, `instance_id`, `stream`, `publication_id`), replay losses by (`node_id`,
   `instance_id`, `stream`, `from_id`, `reason`), gaps by `gap_uid`, events by `event_id`,
   Hub sessions by `session_id`, handoff losses by `loss_id`. Sequence values inside a segment
   can have holes (a row changed before delivery is delivered once, at its latest version).
4. Acknowledge by writing the segment's SHA-256 to `acks/<segment>.ack` through a temporary
   file and a rename in the same directory.
5. Never infer measurements across replay losses, handoff losses, gaps or Hub downtime, and
   never read the Hub database directly.

`tests/test_handoff_consumer.py` contains a minimal reference consumer that does exactly this.
Hub accepts acknowledgements only as a contiguous prefix: an acknowledged later segment does not
count until every earlier one is acknowledged, and a file whose content does not match is
ignored and reported.

## What can be lost, and how it shows

| Situation | Hub behaviour | Visible as |
| --- | --- | --- |
| Raw or history retention deletes a row not yet acknowledged | Deleted as usual | `handoff_losses` row, reason `retention` |
| Raw quota evicts unacknowledged rows (acknowledged ones go first) | Evicted | `handoff_losses`, reason `quota` |
| An unacknowledged segment outlives the shortest data retention | Segment deleted, range resolved | `handoff_losses` with `table_name` `segment:<name>` |
| Rows stored before `--handoff-start now`, or while handoff was disabled | Not handed off | `handoff_losses`, reason `not_handed_off` |
| Spool reaches its quota | Production pauses; collection continues | warning `spool_full` |

Loss rows are themselves delivered in the next segment.

## Monitoring

`GET /api/handoff/status` (and the Data tab) reports the delivered and acknowledged sequence,
segment and spool counts, unacknowledged raw bytes against the raw quota, the oldest
unacknowledged receipt time, losses by table and reason, backfill progress and `warnings`:

| Warning | Act on it |
| --- | --- |
| `spool_full` / `spool_near_quota` | Start or fix the consumer, or raise the spool quota |
| `unacknowledged_raw_near_quota` | Quota eviction will soon delete unacknowledged rows |
| `unacknowledged_near_retention` | Retention will soon delete unacknowledged rows or segments |
| `rejected_acknowledgement` | An `.ack` file does not hold the segment's SHA-256 |
| `error` | See `last_error`; handoff retries every 10 seconds |
