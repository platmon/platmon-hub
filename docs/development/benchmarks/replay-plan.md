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


## CI smoke and host-specific timing

The Ubuntu/Windows Python 3.9/3.13 CI matrix runs the same replay workload with
`--smoke --nodes 10 --seconds 5`. Smoke requires backlog capture, Live collection,
SQLite integrity, encoded queue/RSS bounds where measurable, no Live queue drops,
and no raw admission, cursor-read or commit failures. JSON output names each check
and retains commit/cleanup timings for diagnosis. RSS remains Linux-only.

The writer commits replay pages one at a time, so how many nodes reach their first
page within the window depends on the runner's commit speed. Smoke therefore keeps
running after `--seconds` until every node has committed its first page, for at most
120 s, and reports the extension as `settle_seconds` (`window_seconds` is the window
itself). A node that is never captured still fails `backlog_captured`. The benchmark
mode keeps the fixed window, because capture within it is what it measures.

The default benchmark mode retains the original 250 ms commit/cleanup budgets.
These timed gates belong to controlled host measurements; they are not portable
latency guarantees for shared CI runners. The first Windows 3.13 CI run captured
all ten nodes with no raw failures but measured 735.7 ms commit p95, exceeding the
Linux/WSL2 baseline budget. Smoke success does not establish Windows performance
acceptance; that requires separate measurements on the deployment filesystem.
