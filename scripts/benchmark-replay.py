"""Synthetic replay capacity probe: no sockets, hardware, services or private registry."""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub import protocol
from hub.history.writer import HistoryWriter
from hub.registry import Node
from hub.runtime import Fleet


def rss_bytes():
    if sys.platform.startswith('linux'):
        return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
    return None


def acceptance_checks(result, smoke=False):
    """Portable correctness/resource smoke; timed budgets need a benchmark host."""
    checks = dict(
        backlog_captured=result['captured_nodes'] == result['nodes'],
        live_collected=result['live_nodes'] == result['nodes'],
        integrity=result['integrity'] == 'ok',
        encoded_queue_bounded=result['peak_encoded_queue_bytes'] <= 8*1024**2,
        rss_bounded=result['peak_rss_bytes'] is None or result['peak_rss_bytes'] <= 256*1024**2,
        no_live_queue_drops=result['dropped_queue_full'] == 0,
        no_raw_rejections=result['raw']['rejected'] == 0,
        no_raw_failures=result['raw']['failed_pages'] == 0 and result['raw']['cursor_read_failures'] == 0,
    )
    if not smoke:
        checks['commit_latency'] = result['commit_p95_ms'] is not None and result['commit_p95_ms'] < 250
        checks['cleanup_latency'] = result['cleanup_ms'] < 250
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--nodes', type=int, choices=(10, 50, 100), default=10)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--output', required=True)
    parser.add_argument('--smoke', action='store_true',
                        help='check correctness/resource bounds without host-dependent latency gates')
    args = parser.parse_args()
    if not 3 <= args.seconds <= 60:
        parser.error('--seconds must be 3–60')
    latencies = []
    lock = threading.Lock()
    start = time.monotonic()
    wall = time.time()
    baseline_rss = rss_bytes()
    class Client:
        def get(self, endpoint, path, deadline):
            latest = 120+int(time.monotonic()-start)
            if path == '/api/status':
                return dict(schema_version=1, state='ready', ready=True, instance_id='synthetic', sample={'sequence': latest})
            if path == '/api/stats':
                return dict(schema_version=1, sample=dict(instance_id='synthetic', sequence=latest, data_age_ms=0, stale_after_ms=5000),
                            collectors={'core': {'state': 'ok'}}, cpu=[], memory={})
            return dict(schema_version=2, instance_id='synthetic', groups={})
        def get_replay(self, endpoint, stream, instance, after, deadline):
            if stream != 'core':
                raise protocol.OptionalUnsupported('synthetic group disabled')
            began = time.monotonic()
            latest = 120+int(began-start)
            cursor = min(latest, after+100)
            records = []
            for pid in range(after+1, cursor+1):
                age = max(0, (latest-pid)*1000)
                timing = dict(instance_id=instance, sequence=pid, started_at=wall-120+pid,
                              completed_at=wall-120+pid, duration_ms=0, age_ms=age,
                              data_age_ms=age, stale_after_ms=5000, stale=age>5000)
                data = dict(schema_version=1, sample=timing, collectors={'core': {'state': 'ok'}},
                            cpu=[], sensor_meta={'fixture': 'x'*10000})
                records.append(dict(id=pid, gap_before=False, data=data))
            with lock:
                latencies.append((time.monotonic()-began)*1000)
            return dict(schema_version=1, data_schema_version=1, historical=True, instance_id=instance,
                        stream=stream, clock={'source': 'synthetic'}, retention_s=600, max_bytes=16777216,
                        max_records=4096, max_record_bytes=1048576, max_response_bytes=2097152,
                        oldest_id=1, latest_id=latest, next_after=cursor, has_more=cursor<latest,
                        missing_ranges=[], records=records)
    from hub.history.store import HistoryStore
    commits = []
    class Store(HistoryStore):
        def insert_raw_page(self, *values):
            began = time.monotonic()
            result = super().insert_raw_page(*values)
            commits.append((time.monotonic()-began)*1000)
            return result
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory)/'raw.db'
        writer = HistoryWriter(database, raw_retention_days=1, raw_quota_bytes=1024**3, store_factory=Store)
        nodes = [Node(str(uuid.UUID(int=i+1)), 'synthetic-'+str(i), 'http://192.0.2.'+str(i+1)+':9797') for i in range(args.nodes)]
        fleet = Fleet(nodes, client=Client(), history=writer, max_concurrency=16)
        peak_queue, peak_rss, peak_wal = 0, baseline_rss, 0
        live_polls = 0
        try:
            fleet.start()
            while time.monotonic()-start < args.seconds:
                status = writer.snapshot()
                peak_queue = max(peak_queue, status['raw']['queued_bytes'])
                rss = rss_bytes()
                if rss is not None:
                    peak_rss = max(peak_rss, rss)
                wal = Path(str(database)+'-wal')
                if wal.exists(): peak_wal = max(peak_wal, wal.stat().st_size)
                live_polls = sum(n['last_core_poll_utc'] is not None for n in fleet.list_nodes())
                threading.Event().wait(.05)
        finally:
            fleet.close()
        c = sqlite3.connect(database)
        rows = c.execute('SELECT count(*) FROM raw_publications').fetchone()[0]
        counts = c.execute('SELECT node_id,count(*) FROM raw_publications GROUP BY node_id').fetchall()
        covered = sum(count >= 100 for _, count in counts)
        usage = c.execute('SELECT encoded_bytes FROM raw_usage').fetchone()[0]
        integrity = c.execute('PRAGMA quick_check').fetchone()[0]
        db_size = c.execute('PRAGMA page_count').fetchone()[0]*c.execute('PRAGMA page_size').fetchone()[0]
        prune_start = time.monotonic()
        removed = c.execute('DELETE FROM raw_publications WHERE rowid IN (SELECT rowid FROM raw_publications LIMIT 1000)').rowcount
        c.commit()
        cleanup_ms = (time.monotonic()-prune_start)*1000
        c.close()
        commits.sort()
        p95 = commits[min(len(commits)-1, int(len(commits)*.95))] if commits else None
        result = dict(nodes=args.nodes, seconds=time.monotonic()-start, rows=rows, captured_nodes=covered, encoded_bytes_per_row=usage/rows if rows else None,
                      db_bytes_per_row=db_size/rows if rows else None, peak_encoded_queue_bytes=peak_queue,
                      baseline_rss_bytes=baseline_rss, peak_rss_bytes=peak_rss, peak_wal_bytes=peak_wal,
                      commit_p95_ms=p95, cleanup_rows=removed, cleanup_ms=cleanup_ms,
                      live_nodes=live_polls, integrity=integrity, raw=writer.snapshot()['raw'],
                      dropped_queue_full=writer.snapshot()['dropped_queue_full'],
                      mode='smoke' if args.smoke else 'benchmark',
                      scope='In-process synthetic 10 KiB core payload; no real HTTP, device, concurrent export or operational capacity claim')
        result['checks'] = acceptance_checks(result, args.smoke)
        result['passed'] = all(result['checks'].values())
        Path(args.output).write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(result))
        return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
