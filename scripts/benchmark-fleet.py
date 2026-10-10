"""Bounded loopback-only synthetic fleet benchmark; never contacts real agents."""
import argparse
import ctypes
import json
import math
import multiprocessing as mp
import os
import platform
import random
import selectors
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.history.store import HistoryStore
from hub.history.writer import HistoryWriter
from hub.http import HubServer
from hub.registry import Node
from hub.runtime import Fleet

SCENARIOS = ('normal', 'offline', 'slow', 'timeout', 'max_body', 'db_lock', 'slow_writer', 'overflow', 'alerts')


def distribution(values):
    ordered = sorted(values)
    return dict(count=len(ordered), **{name: round(ordered[min(len(ordered)-1, math.ceil(q*len(ordered))-1)], 3) if ordered else None
        for name, q in (('p50', .5), ('p95', .95), ('p99', .99))})


def rss_mib():
    if os.name == 'nt':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters(); counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize/1048576
    # Benchmark self-observation only, never Agent hardware collection.
    return int(Path('/proc/self/statm').read_text(encoding='utf-8').split()[1])*os.sysconf('SC_PAGE_SIZE')/1048576


def agents(connection, count, scenario):
    started = time.monotonic()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            faulty = self.server.index < max(1, count//10)
            if faulty and scenario == 'offline':
                self.close_connection = True
                return
            if faulty and scenario in ('slow', 'timeout'):
                time.sleep(.5 if scenario == 'slow' else 2.5)
            sequence = int((time.monotonic()-started)*10)+1
            instance = 'synthetic-'+str(self.server.index)
            if self.path == '/api/status':
                doc = dict(schema_version=1, instance_id=instance, ready=True, state='ready', sample={'sequence':sequence})
            elif self.path == '/api/stats':
                doc = dict(schema_version=1, sample=dict(instance_id=instance,sequence=sequence,data_age_ms=1,stale_after_ms=5000),
                    collectors={'core':{'state':'ok'}}, cpu=[{'usage':25}], memory={'used':5,'total':10}, gpu=None, temperature={'synthetic':45})
                if scenario == 'max_body':
                    doc['padding'] = ''
                    doc['padding'] = 'x'*(512*1024-len(json.dumps(doc, separators=(',', ':')).encode()))
            elif self.path == '/api/observations':
                doc = dict(schema_version=2, instance_id=instance, groups={'probe':dict(state='ok',stale_after_ms=20000,
                    observation={'id':sequence,'data_age_ms':1,'stale':False},data={'targets':[]})})
            else:
                self.send_error(404); return
            data = json.dumps(doc, separators=(',', ':')).encode()
            try:
                self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            except (OSError, ConnectionError): pass
    servers, nodes = [], []
    selector = selectors.DefaultSelector()
    try:
        for index in range(count):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.daemon_threads = True
            server.index = index
            servers.append(server)
            selector.register(server, selectors.EVENT_READ)
            nodes.append(Node(str(uuid.UUID(int=index+1)), 'Synthetic '+str(index+1), 'http://127.0.0.1:'+str(server.server_port), True))
        connection.send(nodes)
        while not connection.poll():
            for key, _ in selector.select(.1): key.fileobj._handle_request_noblock()
    finally:
        selector.close()
        for server in servers: server.server_close()
        connection.close()


def measure_hub(connection, nodes, path, seconds, scenario):
    random.seed(42)
    measures = {key:deque(maxlen=20000) for key in ('core_ms', 'healthy_core_ms', 'optional_ms', 'interval_ms', 'healthy_interval_ms', 'worker_wait_ms', 'insert_ms', 'retention_ms')}
    previous = {}
    lock = threading.Lock()
    fault_count = max(1, len(nodes)//10) if scenario in ('offline', 'slow', 'timeout') else 0
    class Store(HistoryStore):
        def insert_batch(self, records):
            began = time.monotonic()
            if scenario in ('slow_writer', 'overflow'): time.sleep(.5 if scenario == 'slow_writer' else 1)
            try: return super().insert_batch(records)
            finally: measures['insert_ms'].append((time.monotonic()-began)*1000)
        def prune(self, *args, **kwargs):
            began = time.monotonic()
            try: return super().prune(*args, **kwargs)
            finally: measures['retention_ms'].append((time.monotonic()-began)*1000)
    class Pool(ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            queued = time.monotonic()
            def run():
                with lock: measures['worker_wait_ms'].append((time.monotonic()-queued)*1000)
                return fn(*args, **kwargs)
            return super().submit(run)
    class MeasuredFleet(Fleet):
        def core_once(self, node_id, state=None):
            began = time.monotonic()
            super().core_once(node_id, state)
            healthy = uuid.UUID(node_id).int > fault_count
            with lock:
                elapsed = (time.monotonic()-began)*1000
                measures['core_ms'].append(elapsed)
                if healthy: measures['healthy_core_ms'].append(elapsed)
                if node_id in previous:
                    interval = (began-previous[node_id])*1000
                    measures['interval_ms'].append(interval)
                    if healthy: measures['healthy_interval_ms'].append(interval)
                previous[node_id] = began
        def observations_once(self, node_id, state=None):
            began = time.monotonic()
            super().observations_once(node_id, state)
            with lock: measures['optional_ms'].append((time.monotonic()-began)*1000)
    writer = HistoryWriter(path, capacity=8 if scenario == 'overflow' else 2048, store_factory=Store)
    from hub.alerts import load_rules
    rules = load_rules(Path(__file__).resolve().parents[1]/'alerts.example.json') if scenario == 'alerts' else None
    fleet = MeasuredFleet(nodes, history=writer, core_interval=.1 if scenario == 'overflow' else 5, observations_interval=10, alert_rules=rules)
    fleet._pool.shutdown()
    fleet._pool = Pool(max_workers=16)
    server = HubServer(('127.0.0.1', 0), fleet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fleet.start()
    connection.send(server.server_port)
    began, cpu = time.monotonic(), time.process_time()
    memory, depth = [], []
    locker = None
    injected = False
    try:
        while time.monotonic()-began < seconds:
            elapsed = time.monotonic()-began
            memory.append((elapsed, rss_mib()))
            depth.append(writer.snapshot()['queue_depth'])
            if scenario == 'db_lock' and not injected and elapsed >= seconds/3 and writer.snapshot()['schema_version'] is not None:
                locker = sqlite3.connect(str(path), isolation_level=None)
                locker.execute('BEGIN IMMEDIATE')
                injected = True
                unlock = elapsed + min(4, seconds/3)
            if locker is not None and elapsed >= unlock:
                locker.execute('ROLLBACK'); locker.close(); locker = None
            time.sleep(.2)
        snapshots = fleet.list_nodes()
        elapsed = time.monotonic()-began
        result = dict(cpu_percent_one_core=round(100*(time.process_time()-cpu)/elapsed,3), elapsed_seconds=round(elapsed,3),
            rss_first_mib=round(memory[0][1],3), rss_last_mib=round(memory[-1][1],3), rss_peak_mib=round(max(v for _,v in memory),3),
            rss_growth_after_warmup_mib=round(memory[-1][1]-next((v for t,v in memory if t>=min(10,seconds/2)),memory[0][1]),3),
            queue_peak=max(depth), live_nodes=sum(n['data_current'] for n in snapshots), nodes=len(nodes))
    finally:
        if locker is not None: locker.execute('ROLLBACK'); locker.close()
        stopping = time.monotonic()
        server.shutdown(); server.server_close(); thread.join(); fleet.close()
    result.update(shutdown_seconds=round(time.monotonic()-stopping,3), writer=writer.snapshot(),
                  distributions={key:distribution(values) for key,values in measures.items()})
    db = sqlite3.connect(str(path))
    try:
        result['core_rows'] = db.execute('SELECT count(*) FROM core_samples').fetchone()[0]
        result['observation_rows'] = db.execute('SELECT count(*) FROM observations').fetchone()[0]
        result['integrity'] = db.execute('PRAGMA quick_check').fetchone()[0]
    finally: db.close()
    result['db_bytes'] = Path(path).stat().st_size
    connection.send(result)
    connection.close()


def run(args):
    context = mp.get_context('spawn')
    with tempfile.TemporaryDirectory(prefix='platmon-fleet-') as directory:
        parent, child = context.Pipe()
        agent = context.Process(target=agents, args=(child,args.nodes,args.scenario))
        agent.start()
        workers = []
        try:
            if not parent.poll(30): raise RuntimeError('synthetic agents did not start')
            nodes = parent.recv()
            runs = []
            for repetition in range(2 if args.restart else 1):
                reader, sender = context.Pipe()
                process = context.Process(target=measure_hub, args=(sender,nodes,Path(directory)/'history.db',args.seconds,args.scenario))
                workers.append(process); process.start()
                if not reader.poll(30): raise RuntimeError('benchmark Hub did not start')
                port = reader.recv()
                latencies = {'api_ms':[], 'history_ms':[]}
                failures = {}
                deadline = time.monotonic()+args.seconds+20
                next_request = time.monotonic()+1
                load_until = time.monotonic()+args.seconds-.4
                while not reader.poll():
                    if time.monotonic() > deadline: raise RuntimeError('benchmark deadline exceeded')
                    if time.monotonic() < next_request or time.monotonic() >= load_until:
                        time.sleep(.02); continue
                    next_request = time.monotonic()+.2
                    for kind, route in (('api_ms','/api/nodes'), ('history_ms','/api/nodes/'+nodes[-1].node_id+'/history?metric=cpu_percent&from_ms=0&to_ms=1')):
                        if kind == 'history_ms':
                            end = int(time.time()*1000)+1
                            route = '/api/nodes/'+nodes[-1].node_id+'/history?metric=cpu_percent&from_ms='+str(end-3600000)+'&to_ms='+str(end)+'&max_points=1200'
                        began = time.monotonic()
                        try:
                            with urllib.request.urlopen('http://127.0.0.1:'+str(port)+route, timeout=3) as response: response.read()
                            latencies[kind].append((time.monotonic()-began)*1000)
                        except (OSError, urllib.error.URLError) as exc:
                            key = str(getattr(exc, 'code', type(exc).__name__))
                            failures[key] = failures.get(key,0)+1
                result = reader.recv(); process.join(timeout=10)
                if process.exitcode != 0: raise RuntimeError('benchmark worker failed')
                result.update(client_distributions={k:distribution(v) for k,v in latencies.items()}, request_failures=failures, restart_generation=repetition)
                runs.append(result)
            output = dict(schema_version=1, synthetic=True, scenario=args.scenario, nodes=args.nodes,
                requested_seconds=args.seconds, python=platform.python_version(), platform=platform.system(), architecture=platform.machine(),
                cpu_count=os.cpu_count(), runs=runs)
            output['checks'] = dict(integrity=all(r['integrity']=='ok' for r in runs),
                healthy_nodes_current=all(r['live_nodes'] >= args.nodes-(max(1,args.nodes//10) if args.scenario in ('offline','timeout') else 0) for r in runs),
                no_normal_drops=all(not any(v for k,v in r['writer'].items() if k.startswith('dropped_')) for r in runs) if args.scenario=='normal' else None,
                restart_rows_preserved=runs[-1]['core_rows'] >= runs[0]['core_rows'] if args.restart else None)
            Path(args.output).write_text(json.dumps(output,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(output))
            if any(value is False for value in output['checks'].values()): raise RuntimeError('benchmark acceptance check failed')
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate()
                worker.join(timeout=5)
            if agent.is_alive(): parent.send('stop')
            agent.join(timeout=5)
            if agent.is_alive(): agent.terminate(); agent.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', type=int, default=10)
    parser.add_argument('--seconds', type=float, default=45)
    parser.add_argument('--scenario', choices=SCENARIOS, default='normal')
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if not 1 <= args.nodes <= 100 or not 2 <= args.seconds <= 1800: parser.error('nodes 1..100; seconds 2..1800')
    run(args)
