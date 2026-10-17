# Replay synthetic acceptance plan

[Benchmark index](README.md) · [ADR-0003](../../design/adr/0003-agent-replay-and-raw-publications.md)

Set these budgets before running `scripts/benchmark-replay.py`: 10/50/100 synthetic
nodes, 120-publication initial core backlog, approximately 10 KiB per publication,
16 shared workers, ten seconds per run. Each node must commit at least its first
100 publications and complete Live collection. SQLite integrity must pass, the
encoded replay queue must stay within 8 MiB, peak Linux process RSS below 256 MiB,
page commit p95 below 250 ms, 1000-row cleanup below 250 ms, and Live history must
have zero queue-overflow drops. These are regression thresholds, not capacity guarantees.

```sh
python3 scripts/benchmark-replay.py --nodes 10 --seconds 10 --output /tmp/replay-10.json
python3 scripts/benchmark-replay.py --nodes 50 --seconds 10 --output /tmp/replay-50.json
python3 scripts/benchmark-replay.py --nodes 100 --seconds 10 --output /tmp/replay-100.json
```

The in-process client avoids sockets and uses synthetic nonroutable registry
endpoints without ever contacting them. It exercises Fleet scheduling, page
validation, queue pressure, writer commits and SQLite. Record encoded row size,
SQLite bytes per row including indexes, peak queue/RSS/WAL, commit latency and
cleanup cost. It excludes HTTP/network costs, real collector payload distributions,
long-term memory, simultaneous exports and real Windows storage behavior. Compare
operational retention/quota choices against actual deployment measurements; no raw
retention or quota default is selected from this short synthetic workload.
