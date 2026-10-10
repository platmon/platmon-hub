"""Opt-in local registry additions; no sessions, discovery or Agent requests."""
import os
import secrets
from pathlib import Path

from .nodes_cli import endpoint_for_host, failure_message, update_registry


class AdminError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class RegistryAdmin:
    def __init__(self, registry_path, token_path):
        self.registry_path = Path(registry_path).resolve()
        path = Path(token_path)
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise ValueError('admin token file must be accessible only to its owner')
        with path.open('rb') as handle:
            raw = handle.read(129)
        token = raw.strip()
        if len(raw) > 128 or len(token) != 64 or any(c not in b'0123456789abcdef' for c in token):
            raise ValueError('admin token file must contain a 64-character lowercase hex token')
        self._token = token

    def authorized(self, authorization):
        if not authorization or not authorization.startswith('Bearer '):
            return False
        try:
            return secrets.compare_digest(authorization[7:].encode('ascii'), self._token)
        except UnicodeError:
            return False

    def add(self, data):
        if (not isinstance(data, dict) or not {'name', 'host'} <= set(data) or
                set(data) - {'name', 'host', 'port', 'enabled'}):
            raise AdminError(400, 'invalid_device', 'Provide name, host and optionally port and enabled.')
        port, enabled = data.get('port', 9797), data.get('enabled', True)
        if (not isinstance(data['name'], str) or not isinstance(data['host'], str) or
                len(data['host']) > 253 or type(port) is not int or not 1 <= port <= 65535 or type(enabled) is not bool):
            raise AdminError(400, 'invalid_device', 'Use a device name, host/IP and port from 1 to 65535.')
        try:
            endpoint = endpoint_for_host(data['host'], port)
            identity = update_registry(self.registry_path, 'add', name=data['name'], endpoint=endpoint, enabled=enabled)
        except (ValueError, UnicodeError) as exc:
            code = 'duplicate_device' if str(exc) == 'duplicate node_id or agent endpoint' else 'invalid_registry'
            raise AdminError(409 if code == 'duplicate_device' else 400, code, failure_message(exc)) from None
        except OSError as exc:
            raise AdminError(503, 'registry_unavailable', failure_message(exc)) from None
        return dict(schema_version=1, saved=True, node_id=identity)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result
