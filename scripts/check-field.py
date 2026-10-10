"""Opt-in read-only field validation using an explicitly supplied private registry.

Starts temporary Hub processes and a temporary database. Never modifies agents,
registry, installed services or an existing Hub. Keep reports outside the repository.
"""
import argparse
import hashlib
import json
import multiprocessing as mp
import platform
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.history.writer import HistoryWriter
from hub.http import HubServer
from hub.registry import load_registry
from hub.runtime import Fleet


def collect(connection, nodes, path, seconds):
    writer = HistoryWriter(path)
    fleet = Fleet(nodes, history=writer)
    server = HubServer(('127.0.0.1',0),fleet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start(); fleet.start()
    connection.send(server.server_port)
    began = time.monotonic()
    try:
        threading.Event().wait(seconds)
        states = fleet.list_nodes()
    finally:
        server.shutdown(); server.server_close(); thread.join(); fleet.close()
    connection.send(dict(elapsed_seconds=round(time.monotonic()-began,3),
        current=[s['data_current'] for s in states], headlines=[s['headline'] for s in states],
        drops=sum(v for k,v in writer.snapshot().items() if k.startswith('dropped_')),writer_state=writer.snapshot()['state']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--restart-seconds', type=float, default=15)
    parser.add_argument('--ready-file')
    args=parser.parse_args()
    if not 10 <= args.seconds <= 1800 or not 10 <= args.restart_seconds <= 300: parser.error('invalid bounded duration')
    registry=Path(args.nodes)
    before=hashlib.sha256(registry.read_bytes()).digest()
    nodes=[node for node in load_registry(registry) if node.enabled]
    if not nodes: parser.error('no enabled registered nodes')
    context=mp.get_context('spawn')
    runs=[]
    with tempfile.TemporaryDirectory(prefix='platmon-field-') as directory:
        db=Path(directory)/'history.db'
        for generation, seconds in enumerate((args.seconds,args.restart_seconds)):
            parent,child=context.Pipe()
            process=context.Process(target=collect,args=(child,nodes,db,seconds))
            process.start()
            try:
                if not parent.poll(30): raise RuntimeError('test Hub startup timeout')
                port=parent.recv()
                if args.ready_file: Path(args.ready_file).write_text(json.dumps({'port':port,'generation':generation}),encoding='utf-8')
                deadline=time.monotonic()+seconds+30
                load_until=time.monotonic()+seconds-.5
                next_read=time.monotonic()+2
                api_reads=0
                while not parent.poll(.1):
                    if time.monotonic()>deadline: raise RuntimeError('test Hub shutdown timeout')
                    if next_read <= time.monotonic() < load_until:
                        with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/api/nodes',timeout=3) as response:
                            payload=json.load(response)
                        if payload['schema_version'] != 1 or len(payload['nodes']) != len(nodes):
                            raise RuntimeError('invalid live API response')
                        api_reads+=1
                        next_read=time.monotonic()+5
                result=parent.recv();process.join(timeout=10)
                result['api_successful_reads']=api_reads
                if process.exitcode != 0: raise RuntimeError('test Hub process failed')
            finally:
                if process.is_alive(): process.terminate();process.join(timeout=5)
            connection=sqlite3.connect(str(db))
            try:
                result['core_rows']=connection.execute('SELECT count(*) FROM core_samples').fetchone()[0]
                result['optional_rows']=connection.execute('SELECT count(*) FROM observations').fetchone()[0]
                result['nodes_with_history']=connection.execute('SELECT count(DISTINCT node_id) FROM core_samples').fetchone()[0]
                result['integrity']=connection.execute('PRAGMA quick_check').fetchone()[0]
                from hub.history.query import query_history
                end=int(time.time()*1000)+1
                result['history_point_counts']=[len(query_history(db,n.node_id,'cpu_percent',end-3600000,end,1200)['points']) for n in nodes]
                result['uuid_match']=set(row[0] for row in connection.execute('SELECT DISTINCT node_id FROM core_samples'))==set(n.node_id for n in nodes)
            finally: connection.close()
            runs.append(result)
    unchanged=hashlib.sha256(registry.read_bytes()).digest()==before
    passed=(unchanged and all(all(r['current']) and r['drops']==0 and r['nodes_with_history']==len(nodes) and
            r['integrity']=='ok' and r['uuid_match'] and r['api_successful_reads']>0 and all(r['history_point_counts']) for r in runs) and
            runs[-1]['core_rows']>runs[0]['core_rows'])
    report=dict(schema_version=1,synthetic=False,platform=platform.system(),python=platform.python_version(),
        enabled_nodes=len(nodes),registry_unchanged=unchanged,runs=runs,passed=passed)
    Path(args.output).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))
    if not passed: raise SystemExit('field validation incomplete; inspect private report')


if __name__=='__main__': main()
