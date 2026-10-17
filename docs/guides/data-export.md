# History data exports

[Documentation index](../README.md) · [User guide](README.md)

History is already persisted in SQLite when `--history-db` is enabled. Exports
provide analysis material; they are not a replacement for SQLite backups.

## CLI and format 1

```powershell
python -m hub export --db "$env:LOCALAPPDATA\platmon-hub\history.db" --output .\measurements.zip --from-ms 1791676800000 --to-ms 1791763200000
```

Both bounds are UTC Unix milliseconds. The interval is half-open `[from_ms,to_ms)`
and at most 31 days. Repeat `--node-id UUID` to select devices, including devices
removed from the current registry. With no filter, all persisted identities in
that period are exported. Names and endpoints are not inferred from today's
registry: the database does not record historical names.

The ZIP contains UTF-8 CSV files with a BOM and CRLF lines:

- `measurements/UUID/YYYY-MM-DD-PART.csv`: original Core rows, split by device,
  UTC receipt date and at most 100,000 rows per part. These are not graph buckets.
- `events.csv`: recorded transitions in the selected interval. Global events are
  included only when no device filter is supplied.
- `gaps.csv`: stored gaps overlapping the interval; original bounds are preserved.
  An empty end is an open gap. Unknown gaps do not prove network failure.
- `observations.csv`: only with `--observations`; selected Storage/Wi-Fi/Probe/Battery
  JSON already persisted by Hub, with no additional Agent requests. Probe data
  can contain private addresses; treat archives as private operational data.
- `manifest.json`: format/schema versions, selection, timestamp/null/unit/text
  contracts, row counts, byte sizes and SHA-256 hashes of every CSV.

The Core columns are `node_id`, `instance_id`, `sequence`, `received_at_ms`,
`data_age_ms`, `stale_after_ms`, `core_state`, `cpu_percent`, `gpu_percent`,
`memory_percent`, `disk_percent`, `hottest_c`, `npu_percent`, `psi_cpu_some_avg10`, `psi_memory_some_avg10`, `psi_io_some_avg10`, `network_rx_busiest_bytes_per_s`, `network_tx_busiest_bytes_per_s`, `disk_read_bytes_per_s`, `disk_write_bytes_per_s`.
Percent values are 0–100; temperature is degrees Celsius; `*_bytes_per_s` columns are
bytes (not bits) per second. Column meanings are in the [history schema](../design/history-schema.md). Receipt time is when Hub observed the sample,
not an exact Agent collection timestamp. Instance changes remain separate rows
and identities. Missing values are empty cells, numeric zero remains zero.

`npu_percent` and the schema-7 load columns are appended to the format-1 CSV columns;
consumers should use header names. Battery JSON retains µAh/µWh, µV, signed µA/µW, and true/false/null for
presence and external supply state. It does not estimate remaining runtime or
convert charge into energy.

To prevent spreadsheet formula execution, risky string cells and strings already
starting with an apostrophe receive one extra leading apostrophe. Numeric cells
(including negative temperatures) are unchanged. A machine reader can reverse
this escape by removing exactly one leading apostrophe from a string cell.
Untrusted strings must still be rendered as text when importing into other tools.

## Bounds and consistency

Exports use a separate read-only SQLite connection and one read transaction:
all members reflect the same committed snapshot while WAL writes continue.
The transaction starts before the first schema/data read. It never changes the
source schema or reads Agent history. A 30-second deadline (including generation),
5,000,000 total-row limit and 512 MiB uncompressed CSV limit bound work. Larger
requests fail; split them by day or device. SQL interruption bounds long scans.
The ZIP is streamed, with one CSV member open at a time.

A temporary file is created beside the destination, then a hard link publishes
the complete archive exclusively. Existing output is never overwritten, even by
competing exporters. Local NTFS and Linux filesystems support this; filesystems
without hard links fail without publishing partial output. Destination parents
must already exist. Failure removes the temporary file. A killed process may
leave a `.platmon-export-*.tmp` file; operators may remove it once no export runs.

Archives contain only data still present at snapshot time. They do not reconstruct
expired samples, unobserved periods or rows lost during Writer failures. Empty
results are valid and described by the manifest. Live network and Disk I/O data
are not currently stored and are not included.

## Implementation stages

1. Versioned CSV/ZIP export CLI.
2. Read-only Dashboard Data tab and bounded downloads.
3. Opt-in daily archives with retention and disk quota.
4. Daily/monthly summaries of observed samples and recorded events/gaps.

Database retention remains separate from archive retention. Automatic archives
must be written before data expires; failures must not block Agent polling.

## Dashboard Data tab

Select **Data**, choose a registered device (or all stored devices), and set From
and To in **UTC**, then download the ZIP. For a removed device, enter its canonical
UUID in the optional field. The manual UUID overrides the selector.

`GET /api/data/export?from_ms=...&to_ms=...&node_id=...` is read-only. The device
filter is optional and independent of current registration. The web path allows
7 days, 200,000 total rows and 32 MiB uncompressed CSV, with a 10-second worker
budget and a 12-second parent deadline. Only one web export runs at a time;
requests during that work return 429. A subprocess isolates generation from the
Polling threads. Its output streams in bounded chunks only after successful
completion; temporary data is removed on completion or disconnected download.
Invalid selections return 400, disabled/unavailable History 503, and failed or
oversized exports 422. Use a smaller range or CLI for larger work.

The browser suppresses duplicate submissions and cancels pending downloads on
leaving the tab or hiding the page. It retains no downloaded archive after the
browser save is initiated. Live Fleet requests are suspended on other tabs;
Agent polling remains active. No directory listing, user-selected output path,
network URL, new write API or external chart service is introduced.

## Automatic daily archives (opt-in)

Start Hub with a dedicated new/empty archive directory:

```powershell
python -m hub --nodes .\nodes.json --history-db "$env:LOCALAPPDATA\platmon-hub\history.db" --archive-dir "$env:LOCALAPPDATA\platmon-hub\archives"
```

Archives are disabled unless `--archive-dir` is supplied. Defaults are
`--archive-retention-days 90` and `--archive-quota-mib 10240` (10 GiB of recognized
ZIPs). The directory is owned by Hub, marked `.platmon-archives-v1` and protected
by a cross-process lock. Do not place unrelated files or manual exports there.
Unrecognized files are never deleted and do not count toward the archive quota;
this is not a quota on the whole disk, SQLite database, or other applications.

Each pass creates at most one complete UTC day's `YYYY-MM-DD.zip` in an isolated
worker. It starts with the oldest available date within the database retention
window (at most 31 days), excluding today. Successful catch-up passes run a minute
apart; caught-up work runs hourly. Errors retry in a minute and are visible through
`GET /api/data/archive/status`; logs contain exception types rather than private
paths or data. Shutdown cancels and waits for the owned worker before closing DB.

Daily files are published only when complete. Age retention removes recognized
old files; quota pressure removes oldest recognized files even within 90 days.
A persisted checkpoint prevents re-creating quota-pruned archives. If one day's
ZIP exceeds the quota or worker budget, it is not published and the checkpoint
does not advance. Increase the budget or split manual exports as appropriate.
Default scheduled files omit optional observations; an explicit standalone
`archive --observations` includes the selected optional data.

For an OS-scheduled job or a manual bounded pass:

```powershell
python -m hub archive --db "$env:LOCALAPPDATA\platmon-hub\history.db" --directory "$env:LOCALAPPDATA\platmon-hub\archives"
```

The standalone command defaults to seven days of catch-up; `--catchup-days` can
be 1–31. It uses the same ownership, checkpoint, retention and quota policy.
Multiple jobs never write concurrently. The normal Windows launcher remains
unchanged and does not silently enable archives.

This is separate from SQLite retention: a failed/offline archiver does not stop
database pruning. Already-expired data cannot be recovered. Dates with no stored
rows produce empty material, not generated samples. A forcibly killed Hub may
leave `.job-*` scratch directories; remove them only after confirming no archive
job is running. Treat the directory as private data and back up important exports.

## Daily/monthly reports

In the Data tab, choose **Daily/monthly report** to download observed resource
summaries instead of raw Core rows. The web limits apply to this download too.
A report ZIP includes:

- `summary-daily.csv` and `summary-monthly.csv`: rows separated by stable node
  UUID and Agent instance. Every metric has non-null count, sample-weighted
  average, minimum and maximum. `sample_count` counts Core rows including rows
  missing that metric. `hottest_at_ms` is the earliest receipt time attaining the
  maximum observed temperature, or empty when temperature is unsupported.
- `event-summary.csv`: counts of recorded transitions by UTC day, node, type,
  previous/current state and reason. Offline/recovery, restarts and Writer errors
  remain distinct. A zero recorded count is not proof that no incident happened.
- `gap-details.csv`: original recorded gap bounds and overlap with the selected
  range, clipped at report generation time for open gaps. Overlapping gaps and
  different scopes must not be summed into an uptime percentage.
- `summary.json`: bounded machine-readable daily material, and `manifest.json`.
  Raw event/gap rows also remain in `events.csv` / `gaps.csv` for investigation.

A selected partial month remains a partial observation window. Average is over
observed samples, not wall-clock time. Neither missing rows nor unknown gaps are
counted as healthy/offline, and null metrics stay null with count zero.

```powershell
python -m hub report --db "$env:LOCALAPPDATA\platmon-hub\history.db" --output .\report.zip --from-ms 1791676800000 --to-ms 1791763200000
```

Use `export --reports` to include raw Core rows and reports in one consistent
snapshot. Reports from SQLite are bounded to 31 days and cannot recover expired
rows. `report --help` describes the database form; the archive form is below.

### Retained daily summaries

Automatic archives also keep `summaries/YYYY-MM-DD.json` separately from raw
ZIPs. Default `--archive-summary-retention-days 365` preserves these daily
summaries for a year; the standalone job uses `--summary-retention-days`.
New summaries use `summary_format_version: 3` with NPU, pressure and I/O rate statistics.
Monthly reports accept formats 1, 2 and 3; metrics an older summary predates (NPU in
format 1, pressure and rates in formats 1 and 2) contribute zero observations and null
aggregates, never an invented idle reading. The raw ZIP format remains 1.
The configured quota now covers recognized ZIPs **and** summaries together.
When it is exhausted, older raw ZIPs are discarded first, then older summaries.
Retention periods are upper bounds, not guaranteed under quota pressure.

Create a monthly report after the raw SQLite/ZIP data has expired:

```powershell
python -m hub report --archive-dir "$env:LOCALAPPDATA\platmon-hub\archives" --month 2026-10 --output .\october-report.zip
```

The command reads retained daily summaries under the archive ownership lock and
merges averages using each metric's non-null sample count. It preserves Agent
instances, extrema, peak times, transition counts and daily gap details. Its
manifest lists every available and missing **completed UTC date** in the month;
future dates and today's unfinished UTC date are not expected. Missing summaries
remain unknown. At most 10,000 metric/event groups, 200,000 output rows, 32 MiB
CSV and a 30-second generation deadline bound work. Invalid/corrupt summaries
fail without publishing a partial report. Existing output is never overwritten.

Summary retention cannot manufacture reports for dates never archived. Earlier
archives made without reports are not silently backfilled. Report snapshots are
analysis material, not DB backups or formal uptime/SLA measurements.

## Complete publication exports

Existing CSV archives and reports contain selected summaries. Opt-in full Agent
publications use a separate bounded `raw-export` JSONL ZIP command, preserving
original collection times, units and cursor/loss evidence. See [raw capture and
export](replay.md). Daily CSV archives do not automatically archive raw publications.
