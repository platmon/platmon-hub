# ADR-0006: Compressed storage of raw publication payloads

[Documentation index](../../README.md) · [Design reference](../README.md)

- **Status:** Implemented in Hub as SQLite schema 8; see [implementation notes](#implementation-notes-2026-10-17)
- **Date:** 2026-10-17
- **Scope:** How `raw_publications.data_json` is stored; amends the storage section of ADR-0003
- **Hub baseline:** `main` at `f4ba1b5e7bb5fe5795462e7ae4b11a5208747d46` (SQLite schema 7)
- **Related:** [ADR-0003](0003-agent-replay-and-raw-publications.md), [ADR-0004](0004-downstream-data-handoff.md), [ADR-0007](0007-sampled-capture-level.md)

## Context

ADR-0003 stores each publication as compact JSON text. On the host measured for ADR-0004
([Capacity](0004-downstream-data-handoff.md#capacity)) a core publication averages 19.4 KB,
about 1.7 GiB per node-day in SQLite at 1 Hz. ADR-0004 limits Hub raw retention to at most
24 hours, but that is still 17 GiB for ten nodes.

Most of each publication repeats the previous one. `sensor_meta` alone is 69 % of it, and its
static part took two distinct values in 268 samples. ADR-0004 named deduplicating that static
part as a follow-up. Measurements on the same 268 core publications compared it with general
compression from the standard library (`zlib`):

| Storage of one core publication | Average size | Reduction | CPU per row |
| --- | --- | --- | --- |
| Compact JSON (current) | 19,373 B | — | — |
| `sensor_meta` static part deduplicated | 9,116 B | 53 % | — |
| Per-row `zlib` level 6 | 5,135 B | 73 % | 0.34 ms |
| Per-row `zlib` level 6 with a shared dictionary (the instance's first publication) | 2,328 B | 88 % | 0.25 ms |
| Static `sensor_meta` deduplication followed by `zlib` | 2,273 B | 88 % | — |
| `zlib` with the previous row as dictionary | 1,206 B | 94 % | — |

Only `hub/raw_export.py` reads `data_json`; nothing queries it with SQLite JSON functions.

## Decision

Store new raw publication payloads as **`zlib`-compressed bytes with a shared,
content-addressed dictionary**, reconstructing the identical compact JSON on read.

1. Add a `raw_dictionaries` table: `dictionary_id`, unique SHA-256 `digest`, `content` (the
   dictionary bytes), `encoded_bytes` and `refs`, maintained by insert/delete triggers exactly
   like `raw_envelopes`.
2. Add nullable `payload_zlib BLOB` and `dictionary_id INTEGER` columns to `raw_publications`.
   A new row stores either `data_json` (legacy and fallback) or `payload_zlib`, never both.
   `dictionary_id` is null for compression without a dictionary.
3. The dictionary for a `(node_id, instance_id, stream)` is the first publication's compact JSON
   stored for that instance, at most 32 KiB (the `zlib` window). Hub selects a new dictionary
   when an instance starts, and when the last 100 rows of that stream averaged more than twice
   the size they had with the current dictionary when it was chosen. A dictionary is never
   changed in place; old rows keep referencing theirs until pruned.
4. Before committing a compressed row, the writer decompresses it and compares the result with
   the original compact JSON. On any mismatch or `zlib` error it stores `data_json` instead and
   counts a `compression_fallback`. Corruption therefore cannot be committed silently.
5. Readers (`raw-export`, ADR-0004 segments) decompress and emit the same JSON as before. No
   export, handoff or HTTP format changes.
6. Existing rows are not rewritten. They expire under raw retention and quota, which keeps the
   migration small and avoids rewriting a multi-gigabyte table inside a backed-up upgrade.

The previous-row dictionary is the smallest but makes every row depend on the row before it, so
quota eviction, retention and partial exports would break chains. Static `sensor_meta`
deduplication saves no more than the shared dictionary when combined with `zlib`, and is specific
to one field of one stream; a shared dictionary needs no knowledge of the payload structure.

## Accounting

The raw quota charges what is stored: for each row, the stored payload bytes (compressed, or
`data_json` for legacy and fallback rows) plus the existing 256-byte row allowance; and each
dictionary once, while referenced, through the `raw_usage` triggers. Envelope accounting is
unchanged. The migration recomputes `raw_usage` in the same transaction that adds the columns.

## Expected effect

With 2.3 KB per core publication plus row overhead, 1 Hz raw storage falls from about 1.7 GiB
to about 0.2 GiB per node-day on the measured host. The ADR-0004 planning figures become:

| Fleet | SQLite raw, 24 h, current | With this decision |
| --- | --- | --- |
| 1 node | 1.7 GiB | about 0.2 GiB |
| 10 nodes | 17 GiB | about 2 GiB |
| 100 nodes | 170 GiB | about 20 GiB |

These figures come from four minutes on one x86_64 host. Dictionary drift over hours, sensor
churn and Jetson or Raspberry Pi payloads are not measured; the re-selection rule exists for
drift, and acceptance requires measuring it.

CPU cost is about 0.25 ms per row to compress and less to verify, on the writer thread: roughly
2.5 % of one core for 100 nodes at 1 Hz. Writer throughput is measured before acceptance.

## Status and diagnostics

`history.raw` gains: rows stored compressed and as JSON, `compression_fallback`, live
dictionaries and their bytes, and the stored-to-logical byte ratio over the last commits.

## Alternatives

| Alternative | Decision |
| --- | --- |
| Deduplicate the static part of `sensor_meta` | Reject: half the saving of compression, specific to one field |
| Per-row `zlib` without a dictionary | Keep as the fallback when no dictionary is suitable; 2.2× larger than with one |
| Chain rows (previous row as dictionary) | Reject: rows stop being independently deletable and exportable |
| `lzma`, `bz2` | Reject: slower per row for small payloads; `zlib` dictionaries fit the 32 KiB payload |
| Third-party `zstd` | Reject: Hub keeps a standard-library runtime |
| Compress the SQLite file or rely on filesystem compression | Reject: outside Hub's control and not portable to Windows defaults |
| Rewrite existing rows | Reject: large migration for data that expires within a day |

## Consequences

Raw storage shrinks by roughly an order of magnitude without changing any external format.
`data_json` is no longer directly readable with SQL tools for new rows; diagnostics and exports
remain the supported way to read raw data, as ADR-0004 already requires for consumers. A
downgrade to a Hub version without this decision cannot read compressed rows; downgrades
continue to require a compatible backup, as for every schema change.

## Implementation and acceptance

1. Migration: dictionary table and triggers, new columns, `raw_usage` recomputation.
2. Writer: dictionary selection and re-selection, compression, round-trip verification and fallback.
3. Readers: `raw-export` and the ADR-0004 segment builder decompress.
4. Diagnostics.

Acceptance covers: byte-identical round trips for core and every slow group; fallback on forced
compression errors; dictionary reference counts under retention, quota eviction and duplicate
retries; quota accounting matching stored bytes; mixed legacy and compressed rows in one export;
migration from schema 7 with existing raw rows; writer throughput at 100 synthetic nodes; and
compression ratio over at least 24 hours on an x86_64 host, a Jetson and a Raspberry Pi, on every
interpreter supported under [ADR-0005](0005-os-and-python-support.md).

## Implementation notes (2026-10-17)

SQLite schema 8 implements the decision. Two details differ from the text above:
`data_json` is `NOT NULL` in schema 6 and SQLite cannot relax it in place, so a compressed
row stores `''` there rather than NULL; and `raw-export` keeps reporting `encoded_bytes` as
the logical compact JSON size plus 256, so the export format is unchanged while the quota
charges stored bytes.

Measured on 2026-10-17 against the installed platmon 1.3.0 service on the x86_64 host, over
four minutes and 1,046 rows (core, storage, Wi-Fi, battery): every row compressed with no
fallback, four dictionaries totalled 23.9 KB, stored payload was 6.4 % of the logical bytes,
and core rows averaged 1,234 bytes. The database file was 1.88 MB for 838 seconds of core
data, about 185 MiB per node-day. Dictionary drift over hours, writer throughput at 100
synthetic nodes and Jetson or Raspberry Pi payloads remain unmeasured acceptance items.
