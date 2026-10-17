"""Opt-in local registry management; no sessions, discovery or Agent requests."""
import json
from pathlib import Path
from dataclasses import asdict
from urllib.parse import urlsplit
from .protocol import unique_object  # noqa: F401  re-exported for hub.http
from .registry import validate_registry

from .nodes_cli import endpoint_for_host, failure_message, update_registry


class AdminError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class RegistryAdmin:
    def __init__(self, registry_path):
        self.registry_path = Path(registry_path).resolve()

    def list_nodes(self):
        try:
            with self.registry_path.open('rb') as handle:
                raw = handle.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('registry exceeds byte limit')
            nodes = validate_registry(json.loads(raw.decode('utf-8')))
            return dict(schema_version=1, nodes=[asdict(node) for node in nodes])
        except (OSError, ValueError, UnicodeError) as exc:
            raise AdminError(503, 'registry_unavailable', failure_message(exc)) from None

    def _device(self, data, updating=False):
        required = {'name', 'host', 'enabled', 'port', 'expected'} if updating else {'name', 'host'}
        allowed = {'name', 'host', 'port', 'enabled', 'expected'} if updating else {'name', 'host', 'port', 'enabled'}
        if not isinstance(data, dict) or not required <= set(data) or set(data) - allowed:
            raise AdminError(400, 'invalid_device', 'Provide name, host, port and enabled; edits also require the loaded device record.')
        port, enabled = data.get('port', 9797), data.get('enabled', True)
        if (not isinstance(data['name'], str) or not isinstance(data['host'], str) or
                len(data['host']) > 253 or type(port) is not int or not 1 <= port <= 65535 or type(enabled) is not bool):
            raise AdminError(400, 'invalid_device', 'Use a device name, host/IP and port from 1 to 65535.')
        try:
            endpoint = endpoint_for_host(data['host'], port)
        except ValueError as exc:
            raise AdminError(400, 'invalid_device', failure_message(exc)) from None
        return dict(name=data['name'], endpoint=endpoint, enabled=enabled)

    def _expected(self, data, node_id):
        expected = data.get('expected') if isinstance(data, dict) else None
        if not isinstance(expected, dict) or set(expected) != {'node_id', 'name', 'endpoint', 'enabled'} or expected['node_id'] != node_id:
            raise AdminError(400, 'invalid_device', 'Refresh Devices and use the loaded device record.')
        try:
            validate_registry({'nodes': [expected]})
        except (ValueError, UnicodeError):
            raise AdminError(400, 'invalid_device', 'Invalid loaded device record.') from None
        return expected

    def _write(self, action, **values):
        try:
            identity = update_registry(self.registry_path, action, **values)
        except (ValueError, UnicodeError) as exc:
            failures = {
                'duplicate node_id or agent endpoint': (409, 'duplicate_device'),
                'unknown node_id': (404, 'unknown_device'),
                'node changed since it was loaded; refresh before retrying': (409, 'device_changed'),
                'registry changed during update; retry from the latest file': (409, 'registry_changed'),
            }
            status, code = failures.get(str(exc), (400, 'invalid_registry'))
            raise AdminError(status, code, failure_message(exc)) from None
        except OSError as exc:
            raise AdminError(503, 'registry_unavailable', failure_message(exc)) from None
        return dict(schema_version=1, saved=True, node_id=identity, action=action)

    def add(self, data):
        return self._write('add', **self._device(data))

    def update(self, node_id, data):
        expected = self._expected(data, node_id)
        device = self._device(data, updating=True)
        # The page edits host and port separately, which drops host case and an
        # implicit :80. Keep the stored spelling so a rename is not an endpoint change.
        if same_origin(device['endpoint'], expected['endpoint']):
            device['endpoint'] = expected['endpoint']
        return self._write('update', node_id=node_id, expected=expected, **device)

    def remove(self, node_id, data):
        if not isinstance(data, dict) or set(data) != {'expected', 'confirm_node_id'} or data.get('confirm_node_id') != node_id:
            raise AdminError(400, 'confirmation_required', 'Confirm the specific device before removing it. History is preserved.')
        return self._write('remove', node_id=node_id, expected=self._expected(data, node_id))


def same_origin(first, second):
    a, b = urlsplit(first), urlsplit(second)
    return (a.hostname, a.port or 80) == (b.hostname, b.port or 80)
