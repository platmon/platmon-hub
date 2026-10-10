"""Bounded long-running Hub observation; real endpoints require an explicit registry.

Field reports stay private. Agents are only read through the existing Fleet GETs.
Uses a temporary Hub/database and never installs services or edits the registry.
"""
import argparse
import hashlib
import importlib
import json
import multiprocessing as mp
import os
import platform
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
benchmark = importlib.import_module('benchmark-fleet')
from hub.registry import load_registry


def write_json(path, payload):
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    os.replace(str(temporary),str(path))


def run(args):
    output=Path(args.output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    progress_path=output.with_name(output.stem+'-progress.jsonl')
    if output.exists() or progress_path.exists(): raise ValueError('output already exists')
    context=mp.get_context('spawn')
    agent=None
    original=None
    if args.registry:
        registry=Path(args.registry)
        original=hashlib.sha256(registry.read_bytes()).digest()
        nodes=[n for n in load_registry(registry) if n.enabled]
        if not nodes: raise ValueError('no enabled nodes')
    with tempfile.TemporaryDirectory(prefix='platmon-soak-') as directory:
        processes=[]
        try:
            if args.synthetic:
                agent_parent,agent_child=context.Pipe()
                agent=context.Process(target=benchmark.agents,args=(agent_child,args.synthetic,'normal'))
                agent.start();processes.append(agent)
                if not agent_parent.poll(30): raise RuntimeError('synthetic startup timeout')
                nodes=agent_parent.recv()
            reader,sender=context.Pipe()
            db=Path(directory)/'history.db'
            worker=context.Process(target=benchmark.measure_hub,
                args=(sender,nodes,db,args.seconds,'normal',args.window_seconds,args.profile))
            worker.start();processes.append(worker)
            if not reader.poll(30): raise RuntimeError('Hub startup timeout')
            port=reader.recv()
            if args.ready_file: write_json(Path(args.ready_file),dict(port=port))
            began=time.monotonic()
            deadline=began+args.seconds+45
            load_until=began+args.seconds-.5
            next_request=began+2
            timings={key:deque(maxlen=20000) for key in ('api_ms','history_ms')}
            failures={}
            counts={key:0 for key in timings}
            windows=[]
            # One fleet and one rotating-device History read per second by default.
            index=0
            while True:
                if reader.poll(.02):
                    message=reader.recv()
                    if message.get('kind')!='progress':
                        result=message
                        break
                    message['client_distributions']={k:benchmark.distribution(v) for k,v in timings.items()}
                    message['request_failures']=dict(failures)
                    message['api_read_counts']=dict(counts)
                    windows.append(message)
                    for values in timings.values(): values.clear()
                    with progress_path.open('a',encoding='utf-8') as handle:
                        handle.write(json.dumps(message)+'\n')
                    write_json(output.with_name(output.stem+'-latest.json'),message)
                    print(json.dumps({k:message[k] for k in ('elapsed_seconds','rss_mib','live_nodes','cpu_percent_one_core','request_failures')}),flush=True)
                tick=time.monotonic()
                if tick>deadline: raise RuntimeError('Hub deadline exceeded')
                if not worker.is_alive(): raise RuntimeError('Hub exited without result')
                if tick<next_request or tick>=load_until: continue
                next_request=tick+1/args.reads_per_second
                end=int(time.time()*1000)+1
                node=nodes[index%len(nodes)];index+=1
                route='/api/nodes/'+node.node_id+'/history?metric=cpu_percent&from_ms='+str(end-3600000)+'&to_ms='+str(end)+'&max_points=1200'
                for kind,path in (('api_ms','/api/nodes'),('history_ms',route)):
                    started=time.monotonic()
                    try:
                        with urllib.request.urlopen('http://127.0.0.1:'+str(port)+path,timeout=3) as response:
                            payload=json.load(response)
                        if payload['schema_version']!=1: raise RuntimeError('unexpected schema')
                        if kind=='api_ms' and {n['node_id'] for n in payload['nodes']}!={n.node_id for n in nodes}:
                            raise RuntimeError('node identity mismatch')
                        if kind=='history_ms' and payload['node_id']!=node.node_id: raise RuntimeError('history identity mismatch')
                        timings[kind].append((time.monotonic()-started)*1000)
                        counts[kind]+=1
                    except (OSError,urllib.error.URLError) as exc:
                        category=str(getattr(exc,'code',type(exc).__name__))
                        failures[category]=failures.get(category,0)+1
            worker.join(timeout=10)
            if worker.exitcode!=0: raise RuntimeError('Hub process failed')
            result['client_final_window']={k:benchmark.distribution(v) for k,v in timings.items()}
            result['request_failures']=failures
            result['api_read_counts']=counts
            unchanged=original is None or hashlib.sha256(registry.read_bytes()).digest()==original
            passed=(result['integrity']=='ok' and result['live_nodes']==len(nodes) and not failures and unchanged and
                not any(v for k,v in result['writer'].items() if k.startswith('dropped_')))
            report=dict(schema_version=1,synthetic=bool(args.synthetic),nodes=len(nodes),
                requested_seconds=args.seconds,window_seconds=args.window_seconds,
                reads_per_second_per_api=args.reads_per_second,profile=args.profile,
                python=platform.python_version(),platform=platform.system(),
                registry_unchanged=unchanged,windows=windows,result=result,passed=passed)
            write_json(output,report)
            print(json.dumps(dict(completed=True,passed=passed,elapsed_seconds=result['elapsed_seconds'])),flush=True)
            if not passed: raise RuntimeError('soak acceptance failed; inspect private report')
        finally:
            if agent is not None and agent.is_alive(): agent_parent.send('stop')
            for process in reversed(processes):
                process.join(timeout=5)
                if process.is_alive():process.terminate();process.join(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    target=parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--synthetic',type=int,help='number of loopback synthetic Agents (1–100)')
    target.add_argument('--registry',help='explicit private registry for read-only real-device checks')
    parser.add_argument('--seconds',type=float,default=3600)
    parser.add_argument('--window-seconds',type=float,default=60)
    parser.add_argument('--reads-per-second',type=float,default=1)
    parser.add_argument('--profile',action='store_true',help='tracemalloc; adds substantial timing overhead')
    parser.add_argument('--output',required=True)
    parser.add_argument('--ready-file')
    args=parser.parse_args()
    if not (10<=args.seconds<=28800 and 2<=args.window_seconds<=3600 and 0.1<=args.reads_per_second<=10):parser.error('invalid bounded timing/load')
    if args.synthetic is not None and not 1<=args.synthetic<=100:parser.error('synthetic count must be 1–100')
    run(args)
