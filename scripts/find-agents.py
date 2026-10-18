"""Find platmon Agents on networks you name, compare them with a registry and optionally register them.

An operator tool, not part of Hub: Hub itself never scans or discovers addresses.
It connects only to the given TCP port(s) and reads the same Agent endpoints Hub reads
(`/api/status`, `/api/stats`), plus the web page footer and `/api/samples` to report the
Agent version and replay support. Nothing is written unless --add is given, and every
addition goes through the same locked, validated registry update as `platmon-hub nodes add`.

    python scripts/find-agents.py                          # this host's own subnets
    python scripts/find-agents.py 192.168.55.0/24 10.0.0.7
    python scripts/find-agents.py --nodes nodes.json --add # confirm each new Agent
"""
import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.nodes_cli import endpoint_for_host, prompt, update_registry
from hub.registry import load_registry

DEFAULT_PORT = 9797
MAX_BODY = 1 << 20
# Hostnames that images ship with; they say nothing about which device it is.
GENERIC_HOSTNAMES = {'localhost', 'ubuntu', 'debian', 'raspberrypi', 'jetson', 'tegra-ubuntu', 'orin', 'nano', 'pi'}
FOOTER = re.compile(r'<footer>\s*platmon v(\d+\.\d+\.\d+)\s*</footer>')
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_networks():
    """This host's directly connected IPv4 networks, or its primary address as a /24 when they cannot be listed."""
    networks = []
    try:
        output = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'],
                                capture_output=True, text=True, timeout=5).stdout
        for line in output.splitlines():
            parts = line.split()
            interface, address = parts[1], parts[3]
            state = Path('/sys/class/net', interface, 'operstate')
            if state.exists() and state.read_text().strip() not in ('up', 'unknown'):
                continue
            networks.append(ipaddress.IPv4Interface(address).network)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        pass
    if not networks:
        # A UDP connect selects the outgoing interface without sending anything.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(('192.0.2.1', 9))
                networks.append(ipaddress.IPv4Interface(probe.getsockname()[0] + '/24').network)
            except OSError:
                pass
    return sorted(set(networks), key=lambda n: (n.network_address, n.prefixlen))


def local_addresses():
    """This host's own addresses, so its Agent matches a loopback registration."""
    addresses = {'127.0.0.1', '::1'}
    try:
        addresses.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        pass
    try:
        output = subprocess.run(['ip', '-o', 'addr'], capture_output=True, text=True, timeout=5).stdout
        addresses.update(part.split('/')[0] for line in output.splitlines() for part in line.split()[3:4])
    except (OSError, subprocess.SubprocessError):
        pass
    return addresses


def parse_targets(values, max_hosts, allow_public):
    """Hosts to probe from CIDRs, single addresses or first-last ranges; refuses oversized or public scans."""
    hosts, labels = [], []
    for value in values:
        try:
            addresses, network_check = expand(value, max_hosts)
        except ValueError as exc:
            raise ValueError(str(exc) if str(exc).startswith(value + ':') else f'{value}: {exc}') from exc
        if addresses is None:
            raise ValueError(f'{value}: more than {max_hosts} addresses; narrow it or raise --max-hosts')
        if not allow_public and not all(a.is_private or a.is_loopback or a.is_link_local for a in network_check):
            raise ValueError(f'{value}: not a private network; pass --allow-public only for networks you operate')
        hosts.extend(addresses)
        labels.append(value)
    unique = list(dict.fromkeys(str(a) for a in hosts))
    if len(unique) > max_hosts:
        raise ValueError(f'{len(unique)} addresses in total; narrow the targets or raise --max-hosts')
    return unique, labels


def expand(value, max_hosts):
    """(addresses, or None when over the limit; the boundary addresses) of one target."""
    if '-' in value and '/' not in value:
        first, last = (ipaddress.ip_address(part.strip()) for part in value.split('-', 1))
        if first.version != last.version or last < first:
            raise ValueError(f'{value}: range must go from a lower to a higher address of one family')
        within = int(last) - int(first) < max_hosts
        return ([type(first)(n) for n in range(int(first), int(last) + 1)] if within else None), [first, last]
    network = ipaddress.ip_network(value, strict=False)
    if network.num_addresses == 1 or (network.version == 4 and network.prefixlen == 31):
        addresses = list(network)
    else:
        addresses = list(network.hosts()) if network.num_addresses <= max_hosts + 2 else None
    return addresses, [network.network_address, network.broadcast_address]


def open_port(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get(origin, path, timeout):
    """(status, body) of one GET; body is parsed JSON for JSON responses, text otherwise."""
    try:
        with OPENER.open(origin + path, timeout=timeout) as response:
            body, status, kind = response.read(MAX_BODY), response.status, response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None
    except (OSError, ValueError):
        return None, None
    text = body.decode('utf-8', 'replace')
    if kind == 'application/json':
        try:
            return status, json.loads(text)
        except ValueError:
            return status, None
    return status, text


def agent_version(origin, status, timeout):
    """Exact version from the web footer when the page is served, otherwise the oldest release with these endpoints."""
    code, page = get(origin, '/', timeout)
    match = FOOTER.search(page) if code == 200 and isinstance(page, str) else None
    if match:
        return match.group(1), True
    if get(origin, '/api/samples?limit=1', timeout)[0] in (200, 400):
        return '1.3.0+', False
    if get(origin, '/api/observations', timeout)[0] == 200:
        return '1.2.0+', False
    return ('1.0.0+' if 'schema_version' in status else 'pre-1.0'), False


def identify(endpoint, timeout):
    """What the Agent at an origin reports about itself, or why it is not one."""
    code, status = get(endpoint, '/api/status', timeout)
    if code is None:
        return dict(endpoint=endpoint, state='no answer')
    if not isinstance(status, dict) or 'ready' not in status:
        return dict(endpoint=endpoint, state='not platmon', http=code)
    _, stats = get(endpoint, '/api/stats', timeout)
    stats = stats if isinstance(stats, dict) else {}
    system = stats.get('system') or {}
    version, exact = agent_version(endpoint, status, timeout)
    replay = get(endpoint, '/api/samples?limit=1', timeout)[0] == 200
    return dict(endpoint=endpoint, state='ready' if status.get('ready') else str(status.get('state') or 'not ready'),
                hostname=system.get('hostname'), platform=stats.get('platform'), model=stats.get('model'),
                os=system.get('os'), arch=system.get('arch'), version=version, version_exact=exact,
                replay=replay, instance_id=status.get('instance_id'),
                supported=not version.startswith('pre-'))


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')


def suggest_name(agent, taken):
    """The hostname unless it is an image default or already used; then platform plus the last address part."""
    hostname = slug(agent.get('hostname'))
    if hostname and hostname not in GENERIC_HOSTNAMES and hostname not in taken:
        return hostname
    host = urlsplit(agent['endpoint']).hostname or ''
    base = slug(agent.get('platform')) or 'agent'
    name, suffix = base + '-' + re.split(r'[.:]', host)[-1], 2
    candidate = name
    while candidate in taken:
        candidate, suffix = f'{name}-{suffix}', suffix + 1
    return candidate


def comparable(endpoint, local):
    """An endpoint key in which this host's own addresses count as loopback."""
    url = urlsplit(endpoint)
    host = url.hostname or ''
    if host in local:
        host = '127.0.0.1'
    return host + ':' + str(url.port or 80)


def address_key(endpoint):
    url = urlsplit(endpoint)
    try:
        address = ipaddress.ip_address(url.hostname)
        return (0, address.version, int(address), url.port or 80)
    except ValueError:
        return (1, 0, url.hostname or '', url.port or 80)


def shorten(text, width=44):
    return text if len(text) <= width else text[:width - 1] + '…'


def scan(hosts, ports, timeout, workers, progress=None):
    pairs = [(host, port) for host in hosts for port in ports]
    found, done = [], 0
    with ThreadPoolExecutor(workers) as pool:
        for (host, port), is_open in zip(pairs, pool.map(lambda pair: open_port(*pair, timeout), pairs)):
            done += 1
            if progress and (done % 64 == 0 or done == len(pairs)):
                progress(done, len(pairs), len(found) + is_open)
            if is_open:
                found.append(endpoint_for_host(host, port))
    return found


def survey(hosts, ports, registry_nodes, timeout, http_timeout, workers, progress=None, local=None):
    """Agents found on the targets plus every registered endpoint, each marked against the registry."""
    local = local_addresses() if local is None else local
    endpoints = scan(hosts, ports, timeout, workers, progress)
    registered = {comparable(node.endpoint, local): node for node in registry_nodes}
    seen = {comparable(endpoint, local) for endpoint in endpoints}
    # Registered endpoints are always checked, also outside the scanned networks.
    endpoints += [node.endpoint for key, node in registered.items() if key not in seen]
    with ThreadPoolExecutor(min(workers, 32)) as pool:
        agents = list(pool.map(lambda endpoint: identify(endpoint, http_timeout), endpoints))
    for agent in agents:
        node = registered.get(comparable(agent['endpoint'], local))
        agent['this_host'] = (urlsplit(agent['endpoint']).hostname or '') in local
        if node:
            agent.update(registry='registered' if node.enabled else 'registered, disabled', node_id=node.node_id, name=node.name)
        else:
            agent['registry'] = 'new' if agent['state'] not in ('no answer', 'not platmon') else '-'
    order = {'new': 0, 'registered': 1, 'registered, disabled': 2, '-': 3}
    agents.sort(key=lambda a: (order.get(a['registry'], 3), a['state'] == 'no answer', address_key(a['endpoint'])))
    return agents


def table(agents):
    columns = [('ENDPOINT', lambda a: a['endpoint'] + (' (this host)' if a.get('this_host') else '')),
               ('REGISTRY', lambda a: a['registry'] + (f" as {a['name']}" if a.get('name') else '')),
               ('STATE', lambda a: a['state']),
               ('HOSTNAME', lambda a: a.get('hostname') or ''),
               ('DEVICE', lambda a: shorten(a.get('model') or a.get('platform') or '')),
               ('OS', lambda a: ' '.join(filter(None, [a.get('os'), a.get('arch')]))),
               ('VERSION', lambda a: a.get('version') or ''),
               ('REPLAY', lambda a: '' if 'replay' not in a else ('yes' if a['replay'] else 'no'))]
    rows = [[name for name, _ in columns]] + [[str(cell(a)) for _, cell in columns] for a in agents]
    widths = [max(len(row[i]) for row in rows) for i in range(len(columns))]
    return '\n'.join('  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows)


def add_agents(agents, registry, assume_yes, ask=prompt, out=sys.stderr):
    """Register new Agents one by one after confirmation; returns the added (name, endpoint, node_id)."""
    taken = {slug(node.name) for node in load_registry(registry)} if Path(registry).exists() else set()
    added = []
    for agent in (a for a in agents if a['registry'] == 'new'):
        name = suggest_name(agent, taken)
        if not agent['supported']:
            print(f"skip {agent['endpoint']}: Agent {agent['version']} is older than Hub supports", file=out)
            continue
        if not assume_yes:
            print(f"\n{agent['endpoint']}  {agent.get('model') or agent.get('platform') or ''}  "
                  f"hostname {agent.get('hostname') or '?'}  platmon {agent['version']}", file=out)
            try:
                answer = ask('Name, or "skip"', name)
            except (EOFError, KeyboardInterrupt):
                print('\nStopped; nothing more is added.', file=out)
                break
            if answer.lower() in ('skip', 's', 'n', 'no'):
                continue
            name = answer
        node_id = update_registry(registry, 'add', name=name, endpoint=agent['endpoint'])
        taken.add(slug(name))
        added.append((name, agent['endpoint'], node_id))
        print(f"added {name} {agent['endpoint']} {node_id}", file=out)
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('targets', nargs='*', help='CIDR, address or first-last range; default: this host\'s own IPv4 networks')
    parser.add_argument('--port', type=int, action='append', help=f'Agent port to try (repeatable, default {DEFAULT_PORT})')
    parser.add_argument('--nodes', help='registry JSON to compare with; its endpoints are always checked')
    parser.add_argument('--add', action='store_true', help='register new Agents in --nodes, asking for each one')
    parser.add_argument('--yes', action='store_true', help='with --add, register every new Agent under its suggested name')
    parser.add_argument('--json', action='store_true', help='print JSON instead of a table')
    parser.add_argument('--all', action='store_true', help='also list open ports that are not platmon')
    parser.add_argument('--timeout', type=float, default=0.5, help='TCP connect timeout in seconds (default 0.5)')
    parser.add_argument('--http-timeout', type=float, default=3.0, help='per-request HTTP timeout in seconds (default 3)')
    parser.add_argument('--workers', type=int, default=128, help='parallel connection attempts (default 128)')
    parser.add_argument('--max-hosts', type=int, default=1024, help='refuse to probe more addresses than this (default 1024)')
    parser.add_argument('--allow-public', action='store_true', help='allow non-private addresses')
    args = parser.parse_args(argv)
    if args.add and not args.nodes:
        parser.error('--add needs --nodes')
    if args.yes and not args.add:
        parser.error('--yes only applies with --add')
    if not 1 <= args.workers <= 1024 or args.timeout <= 0 or args.http_timeout <= 0:
        parser.error('--workers must be 1 to 1024 and timeouts positive')
    ports = sorted(set(args.port or [DEFAULT_PORT]))
    if any(not 1 <= port <= 65535 for port in ports):
        parser.error('--port must be 1 to 65535')
    targets = args.targets or [str(network) for network in local_networks()]
    if not targets:
        parser.error('could not determine this host\'s networks; name them, e.g. 192.168.1.0/24')
    if not args.targets:
        print('Scanning this host\'s networks: ' + ', '.join(targets), file=sys.stderr)
    try:
        hosts, _ = parse_targets(targets, args.max_hosts, args.allow_public)
        nodes = list(load_registry(args.nodes)) if args.nodes and Path(args.nodes).exists() else []
    except ValueError as exc:
        parser.error(str(exc))
    interactive = sys.stderr.isatty() and not args.json

    def progress(done, total, hits):
        print(f'\r{done}/{total} probed, {hits} open', end='' if done < total else '\n', file=sys.stderr, flush=True)

    began = time.monotonic()
    agents = survey(hosts, ports, nodes, args.timeout, args.http_timeout, args.workers, progress if interactive else None)
    shown = agents if args.all else [a for a in agents if a['state'] != 'not platmon']
    if args.json:
        print(json.dumps(dict(targets=targets, ports=ports, probed=len(hosts) * len(ports),
                              seconds=round(time.monotonic() - began, 1), agents=shown), indent=2, ensure_ascii=False))
    else:
        print(table(shown) if shown else 'No platmon Agent found.', flush=True)
        new = sum(a['registry'] == 'new' for a in agents)
        missing = sum(a['registry'].startswith('registered') and a['state'] == 'no answer' for a in agents)
        print(f"\n{len(hosts) * len(ports)} addresses probed in {time.monotonic() - began:.1f} s: "
              f"{sum(a['state'] not in ('no answer', 'not platmon') for a in agents)} Agents, {new} not registered"
              + (f', {missing} registered not answering' if missing else ''), file=sys.stderr)
        if new and args.nodes and not args.add:
            print(f'Register them with: --nodes {args.nodes} --add', file=sys.stderr)
    if args.add:
        if not args.yes and not sys.stdin.isatty():
            parser.error('--add asks for each Agent; use --yes when there is no terminal')
        add_agents(agents, args.nodes, args.yes)
    return 0


if __name__ == '__main__':
    sys.exit(main())
