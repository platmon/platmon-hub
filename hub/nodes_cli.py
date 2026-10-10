"""Local registry administration. Never contacts an agent or alters history."""

import argparse
import json
import os
import stat
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

from .filelock import FileLock
from .registry import load_registry, validate_registry


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
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def update_registry(path, action, *, node_id=None, name=None, endpoint=None, enabled=True):
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
            if action == 'rename': item['name'] = name
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog='platmon-hub nodes', description='Local registry management; no agent writes')
    parser.add_argument('--file', required=True, help='registry JSON path')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('list')
    commands.add_parser('validate')
    add = commands.add_parser('add')
    add.add_argument('--name', required=True)
    add.add_argument('--endpoint', required=True)
    add.add_argument('--disabled', action='store_true')
    for action in ('rename', 'set-endpoint', 'enable', 'disable', 'remove'):
        command = commands.add_parser(action)
        command.add_argument('node_id')
        if action == 'rename': command.add_argument('name')
        if action == 'set-endpoint': command.add_argument('endpoint')
    args = parser.parse_args(argv)
    try:
        if args.action in ('list', 'validate'):
            nodes = load_registry(args.file)
            print(json.dumps({'nodes': [asdict(n) for n in nodes]} if args.action == 'list' else
                             {'valid': True, 'node_count': len(nodes)}, ensure_ascii=False))
        else:
            identity = update_registry(args.file, args.action, node_id=getattr(args, 'node_id', None),
                                       name=getattr(args, 'name', None), endpoint=getattr(args, 'endpoint', None),
                                       enabled=not getattr(args, 'disabled', False))
            print(json.dumps({'action': args.action, 'node_id': identity}))
    except (OSError, ValueError, UnicodeError) as exc:
        # Do not include raw file content or endpoint credentials in errors.
        parser.exit(2, 'Registry update failed: ' + type(exc).__name__ + '. Check file permissions, lock ownership and registry validation.\n')
