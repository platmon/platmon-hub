"""Local registry administration. Never contacts an agent or alters history."""

import argparse
import ipaddress
import json
import os
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .filelock import FileLock
from .registry import load_registry, validate_registry


def replace(source, target, attempts=20, delay=0.05):
    """Windows refuses to replace a file another process (editor, antivirus,
    the Hub watcher) has open; that sharing violation clears within moments."""
    for attempt in range(attempts):
        try:
            os.replace(str(source), str(target))
            return
        except PermissionError:
            if os.name != 'nt' or attempt == attempts - 1:
                raise
            time.sleep(delay)


def atomic_write(path, data, mode=0o600):
    """Replace one file only after its complete contents reach the local filesystem."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temporary), mode)
        replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def update_registry(path, action, *, node_id=None, name=None, endpoint=None, enabled=True, expected=None):
    path = Path(path).resolve()
    with FileLock(str(path) + '.lock'):
        original = path.read_bytes() if path.exists() else None
        if original is None and action != 'add':
            raise ValueError('registry does not exist')
        nodes = [] if original is None else list(validate_registry(json.loads(original.decode('utf-8'))))
        records = [asdict(node) for node in nodes]
        if action == 'add':
            node_id = str(uuid.uuid4())
            records.append(dict(node_id=node_id, name=name, endpoint=endpoint, enabled=enabled))
        else:
            item = next((r for r in records if r['node_id'] == node_id), None)
            if item is None:
                raise ValueError('unknown node_id')
            if expected is not None and item != expected:
                raise ValueError('node changed since it was loaded; refresh before retrying')
            if action == 'update': item.update(name=name, endpoint=endpoint, enabled=enabled)
            elif action == 'rename': item['name'] = name
            elif action == 'set-endpoint': item['endpoint'] = endpoint
            elif action in ('enable', 'disable'): item['enabled'] = action == 'enable'
            elif action == 'remove': records.remove(item)
            else: raise ValueError('unknown registry action')
        result = validate_registry({'nodes': records})
        data = (json.dumps({'nodes': [asdict(node) for node in result]}, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
        # Cooperative CLI writers share the lock; detect intervening manual edits too.
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError('registry changed during update; retry from the latest file')
        mode = stat.S_IMODE(path.stat().st_mode) if original is not None else 0o600
        if original is not None:
            atomic_write(path.with_name(path.name + '.bak'), original, mode)
        atomic_write(path, data, mode)
        return node_id


def agent_port(value):
    try:
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError()
        return port
    except ValueError as exc:
        raise argparse.ArgumentTypeError('agent port must be an integer from 1 to 65535') from exc


def endpoint_for_host(host, port):
    """Construct an explicit origin; never infer, resolve or discover an address."""
    if not host or any(c.isspace() for c in host) or any(c in host for c in '/?#@'):
        raise ValueError('enter a host or IP only; use --endpoint for a full HTTP origin')
    if ':' in host:
        address = host[1:-1] if host.startswith('[') and host.endswith(']') else host
        try:
            ipaddress.IPv6Address(address)
        except ValueError as exc:
            raise ValueError('use --port for the agent port; bracketed or bare IPv6 is supported') from exc
        host = '[' + address + ']'
    elif '[' in host or ']' in host:
        raise ValueError('enter a host or IP only; use --endpoint for a full HTTP origin')
    return 'http://' + host + ':' + str(port)


def prompt(label, default=None):
    suffix = ' [' + str(default) + ']' if default is not None else ''
    print(label + suffix + ': ', end='', file=sys.stderr, flush=True)
    value = input().strip()
    return value if value else default


def add_arguments(args, parser):
    if args.port is not None and args.endpoint is not None:
        parser.error('--port is used with --host; --endpoint already contains its port')
    if not args.interactive and (args.name is None or (args.host is None and args.endpoint is None)):
        parser.error('add requires --name and --host or --endpoint; use --interactive for guided registration')
    if args.interactive:
        try:
            if args.name is None:
                args.name = prompt('Device name')
            if args.host is None and args.endpoint is None:
                args.host = prompt('Agent IP or hostname')
            if args.endpoint is None and args.port is None:
                args.port = agent_port(prompt('Agent port', 9797))
        except (EOFError, KeyboardInterrupt):
            parser.exit(2, '\nRegistration canceled; no registry changes.\n')
    if args.endpoint is None:
        args.endpoint = endpoint_for_host(args.host, args.port if args.port is not None else 9797)
    # Validate before displaying the local preview, including credential rejection.
    validate_registry({'nodes': [dict(node_id=str(uuid.uuid4()), name=args.name,
        endpoint=args.endpoint, enabled=not args.disabled)]})
    if args.interactive:
        print(json.dumps(dict(name=args.name, endpoint=args.endpoint, enabled=not args.disabled),
                         ensure_ascii=False), file=sys.stderr)
        try:
            confirmed = prompt('Save this device? (yes/no)', 'no')
        except (EOFError, KeyboardInterrupt):
            parser.exit(2, '\nRegistration canceled; no registry changes.\n')
        if confirmed.lower() not in ('y', 'yes'):
            parser.exit(0, 'Registration canceled; no registry changes.\n')


def failure_message(exc):
    """Return fixed guidance only; never echo file contents, paths or credentials."""
    safe_messages = {
        'duplicate node_id or agent endpoint': 'A node with this ID or endpoint already exists. Use list, then set-endpoint or rename the existing UUID.',
        'registry exceeds 256 nodes': 'The registry limit is 256 nodes. Remove an unused entry before adding another.',
        'node name must be nonempty and at most 100 characters': 'Use a nonempty device name of at most 100 characters.',
        'agent endpoint must be a plain HTTP origin without credentials or path': 'Use a plain http://host:port origin without credentials, path, query or fragment.',
        'invalid agent port': 'Use an agent port from 1 to 65535.',
        'node changed since it was loaded; refresh before retrying': 'This device changed since it was loaded. Refresh Devices before retrying.',
        'unknown node_id': 'Unknown node_id. Use nodes list to obtain the existing UUID.',
        'registry does not exist': 'Registry does not exist. Use add to create it in an existing directory.',
        'registry changed during update; retry from the latest file': 'Registry changed during update. Retry from the latest file.',
        'enter a host or IP only; use --endpoint for a full HTTP origin': 'Enter only a host or IP with --host; use --endpoint for a full HTTP origin.',
        'use --port for the agent port; bracketed or bare IPv6 is supported': 'Use --port separately from --host. Bare or bracketed IPv6 is supported.',
    }
    if isinstance(exc, argparse.ArgumentTypeError):
        return 'Use an agent port from 1 to 65535.'
    if isinstance(exc, FileNotFoundError):
        return 'Registry or parent directory does not exist. Check --file and create its parent directory.'
    return safe_messages.get(str(exc), 'Check file permissions, lock ownership and registry validation.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='platmon-hub nodes', description='Local registry management; no agent writes')
    parser.add_argument('--file', required=True, help='registry JSON path')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('list')
    commands.add_parser('validate')
    add = commands.add_parser('add')
    add.add_argument('--name', help='display name (required unless --interactive)')
    address = add.add_mutually_exclusive_group()
    address.add_argument('--endpoint', help='complete plain HTTP origin')
    address.add_argument('--host', help='Agent IP or hostname; default port 9797')
    add.add_argument('--port', type=agent_port, help='Agent port for --host (default: 9797)')
    add.add_argument('--interactive', action='store_true', help='prompt locally and confirm before saving')
    add.add_argument('--disabled', action='store_true')
    for action in ('rename', 'set-endpoint', 'enable', 'disable', 'remove'):
        command = commands.add_parser(action)
        command.add_argument('node_id')
        if action == 'rename': command.add_argument('name')
        if action == 'set-endpoint': command.add_argument('endpoint')
    args = parser.parse_args(argv)
    try:
        if args.action == 'add':
            add_arguments(args, parser)
        if args.action in ('list', 'validate'):
            nodes = load_registry(args.file)
            print(json.dumps({'nodes': [asdict(n) for n in nodes]} if args.action == 'list' else
                             {'valid': True, 'node_count': len(nodes)}, ensure_ascii=False))
        else:
            identity = update_registry(args.file, args.action, node_id=getattr(args, 'node_id', None),
                                       name=getattr(args, 'name', None), endpoint=getattr(args, 'endpoint', None),
                                       enabled=not getattr(args, 'disabled', False))
            print(json.dumps({'action': args.action, 'node_id': identity}))
    except (OSError, ValueError, UnicodeError, argparse.ArgumentTypeError) as exc:
        # Do not include raw file content or endpoint credentials in errors.
        parser.exit(2, 'Registry update failed: ' + failure_message(exc) + '\n')
