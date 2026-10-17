# Raw replay synthetic results

[Benchmark index](README.md) · [Pre-run plan](replay-plan.md) · [Machine-readable aggregates](replay-summary.json)

Measured 2026-10-17 on Linux/WSL2, Python 3.12.3, one run at a time without another
test/build workload. Each run used approximately 10 KiB synthetic core publications,
a 120-publication initial backlog, the 8 MiB encoded queue, 16 shared workers,
and a ten-second collection window. No real Agent, registry or service was used.

| Nodes | Raw rows | Nodes with ≥100 rows | Peak RSS MiB | Peak encoded queue MiB | Commit p95 ms | Cleanup 1000 rows ms |
| --- | --- | --- | --- | --- | --- | --- |
| 10 | 1260 | 10 | 41.54 | 2.02 | 20.80 | 44.70 |
| 50 | 6341 | 50 | 50.04 | 3.02 | 23.46 | 46.04 |
| 100 | 10000 | 100 | 52.80 | 3.02 | 21.18 | 45.47 |

All Live nodes collected, SQLite integrity was `ok`, no Live queue overflows
occurred, and all pre-run thresholds passed. Raw admission rejections and failed
commits were zero. Encoded storage charged about 10866 bytes/row; total SQLite
pages including indexes were about 12478–12555 bytes/row. Peak WAL was about
5.05–5.16 MiB. Deletion time includes updating transactional byte-accounting triggers;
it does not include VACUUM, checkpoint or deletion of files on the filesystem.

The probe exposed queue pressure when catch-up workers fetched pages before
reserving capacity. Collector reservations now admit a worst-case 3 MiB page and
one command slot before the GET. Registry invalidation and request failure release
that reservation. Cursor reads share bounded writer turns, and pending raw work
does not incur an idle 50 ms Live-queue wait. Due Live work is still admitted first.
Before those changes, the 100-node ten-second catch-up target was not met. A run
concurrent with the package/full-test workload also missed the commit target; those
runs are not represented as passing operational evidence.

These measurements cover Python/Fleet/validation/SQLite, not real HTTP costs,
maximal payloads, prolonged outages, real Windows filesystems, seven-day data,
simultaneous viewers/exports or indefinite memory stability. At 100 nodes only the
first 100 publications per node committed within this window; it does not prove
that the complete 120-record backlog was drained in ten seconds. Explicit raw
retention and quota still require deployment measurements. Existing operational
release gates remain open.
