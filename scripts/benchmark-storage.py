"""Measure current-schema insert, query, retention and checkpoint costs in a temp DB."""
import json
import tempfile
import time
import uuid
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.history.projector import CoreRecord, ObservationRecord
from hub.history.query import query_history
from hub.history.store import HistoryStore


def main():
    result = {}
    with tempfile.TemporaryDirectory(prefix='platmon-storage-') as directory:
        path = Path(directory)/'history.db'
        store = HistoryStore(path).open()
        try:
            store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            empty = path.stat().st_size
            now = int(time.time()*1000)
            began = time.monotonic()
            # 100 nodes x 1,000 samples at 5s; 500 optional observations at 10s.
            batch = []
            for sequence in range(1,1001):
                for index in range(100):
                    node = str(uuid.UUID(int=index+1))
                    stamp = now-(1000-sequence)*5000
                    batch.append(CoreRecord(node,'synthetic',sequence,stamp,1,5000,'ready',25,None,50,None,45))
                    if sequence%2 == 0:
                        batch.append(ObservationRecord(node,'synthetic','storage',sequence//2,stamp,1,20000,'ok',
                            json.dumps({'filesystems':[{'device':'synthetic','total_bytes':1000000000,'used_bytes':500000000,'available_bytes':500000000}]})))
                    if len(batch)>=128: store.insert_batch(batch); batch=[]
            if batch: store.insert_batch(batch)
            result['insert_seconds'] = round(time.monotonic()-began,3)
            began = time.monotonic()
            store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            result['checkpoint_ms'] = round((time.monotonic()-began)*1000,3)
            result.update(core_rows=100000, optional_rows=50000, db_bytes=path.stat().st_size,
                          incremental_bytes_per_core_plus_half_optional=round((path.stat().st_size-empty)/100000,2))
            began = time.monotonic()
            # Every node has 1,000 samples, requiring aggregation to 100 points.
            response = query_history(path,str(uuid.UUID(int=1)),'cpu_percent',now-5000000,now+1,100)
            result['query_ms'] = round((time.monotonic()-began)*1000,3)
            result['query_points'] = len(response['points'])
            began = time.monotonic(); deleted=0; chunks=0
            while True:
                count = store.prune(now+1, event_cutoff_ms=now+1)
                deleted += count; chunks += 1
                if not count: break
            result.update(retention_ms=round((time.monotonic()-began)*1000,3),retention_deleted=deleted,retention_chunks=chunks)
            result['integrity'] = store.connection.execute('PRAGMA quick_check').fetchone()[0]
        finally: store.close()
    Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__': main()
