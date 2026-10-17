"""platmon v1.0.0–v1.2.0 endpoint validation and bounded GET."""

import json
import math
import socket
import urllib.error
import urllib.request
from urllib.parse import urlencode


class AgentFailure(Exception):
    category = "error"


class TransportFailure(AgentFailure):
    category = "transport"


class ServerFailure(AgentFailure):
    category = "error"


class ContractFailure(AgentFailure):
    category = "incompatible"


class ProtocolFailure(AgentFailure):
    category = "error"


class NoCurrentData(AgentFailure):
    category = "stale"


class OptionalUnsupported(AgentFailure):
    """Older agents answer 404 for an optional endpoint; not an error."""
    category = "unsupported"


class ReplayConflict(AgentFailure):
    def __init__(self, code, instance_id):
        super().__init__(code)
        self.code, self.instance_id = code, instance_id


def decode_json(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(content.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda name: (_ for _ in ()).throw(ValueError('non-finite JSON token')))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ProtocolFailure('invalid JSON') from exc


def _nonnegative(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def _schema(document, expected):
    if not isinstance(document, dict):
        raise ProtocolFailure("response is not an object")
    if type(document.get("schema_version")) is not int or document["schema_version"] != expected:
        raise ContractFailure(f"unsupported endpoint schema (expected {expected})")


def status(document):
    _schema(document, 1)
    state, ready = document.get("state"), document.get("ready")
    if type(ready) is not bool or state not in ("ready", "degraded", "starting", "stale") or ready != (state in ("ready", "degraded")):
        raise ProtocolFailure("invalid state/ready combination")
    if not isinstance(document.get("instance_id"), str) or not document["instance_id"]:
        raise ProtocolFailure("missing instance_id")
    if "sample" not in document:
        raise ProtocolFailure("missing status sample")
    sample = document["sample"]
    if ready and sample is None:
        raise ProtocolFailure("ready status missing sample")
    if sample is not None and (not isinstance(sample, dict) or
                               type(sample.get("sequence")) is not int or sample["sequence"] < 1):
        raise ProtocolFailure("invalid status sample")
    return document


def stats(document, instance_id):
    _schema(document, 1)
    sample = document.get("sample")
    if not isinstance(sample, dict) or sample.get("instance_id") != instance_id:
        raise ProtocolFailure("stats instance differs from status")
    if type(sample.get("sequence")) is not int or sample["sequence"] < 1:
        raise ProtocolFailure("invalid sample sequence")
    if not _nonnegative(sample.get("data_age_ms")) or not _nonnegative(sample.get("stale_after_ms")) or sample["stale_after_ms"] <= 0:
        raise ProtocolFailure("invalid sample age/deadline")
    collectors = document.get("collectors")
    if not isinstance(collectors, dict) or not isinstance(collectors.get("core"), dict) or collectors["core"].get("state") != "ok":
        raise ProtocolFailure("missing/invalid core collector")
    return document


def observations(document, instance_id):
    _schema(document, 2)
    if document.get("instance_id") != instance_id or not isinstance(document.get("groups"), dict):
        raise ProtocolFailure("invalid observations envelope")
    for key, group in document["groups"].items():
        if not isinstance(key, str) or not isinstance(group, dict) or group.get("state") not in ("starting", "stale", "ok", "partial", "error", "unavailable"):
            raise ProtocolFailure("invalid observation group")
        if not _nonnegative(group.get("stale_after_ms")):
            raise ProtocolFailure("invalid observation stale threshold")
        observation = group.get("observation")
        if observation is not None and (not isinstance(observation, dict) or type(observation.get("id")) is not int or observation["id"] < 1 or not _nonnegative(observation.get("data_age_ms"))):
            raise ProtocolFailure("invalid observation metadata")
    return document


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProtocolFailure("redirect disallowed")


class HttpAgentClient:
    """Fixed GET-only paths; bounded response size; no redirects or auto proxy."""

    def __init__(self, max_bytes=512 * 1024):
        self.max_bytes = max_bytes
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def get(self, endpoint, path, deadline):
        if path not in ("/api/status", "/api/stats", "/api/observations"):
            raise ValueError("path is not an allowed read-only endpoint")
        return self._get(endpoint, path, deadline, self.max_bytes)

    def get_replay(self, endpoint, stream, instance_id, after, deadline, limit=100):
        from .history.raw import STREAMS, integer, instance
        if stream not in STREAMS or not instance(instance_id) or not integer(after) or not integer(limit, 1, 100):
            raise ValueError('invalid replay request')
        params = dict(instance_id=instance_id, after=after, limit=limit)
        path = '/api/samples'
        if stream != 'core':
            path = '/api/observation-samples'
            params['group'] = stream
        return self._get(endpoint, path + '?' + urlencode(params), deadline, 2*1024**2, replay=True)

    def _get(self, endpoint, path, deadline, max_bytes, replay=False):
        request = urllib.request.Request(endpoint + path, method="GET", headers={"Accept": "application/json", "Cache-Control": "no-store"})
        try:
            with self.opener.open(request, timeout=deadline) as response:
                if response.status != 200:
                    raise ServerFailure("unexpected HTTP status")
                content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise ProtocolFailure("response exceeds byte limit")
        except urllib.error.HTTPError as exc:
            if replay and exc.code in (404, 409):
                with exc:
                    body = exc.read(4097)
                if len(body) > 4096:
                    raise ProtocolFailure('replay error exceeds byte limit') from exc
                if exc.code == 404:
                    raise OptionalUnsupported('replay stream unavailable') from exc
                data = decode_json(body)
                from .history.raw import instance
                if (not isinstance(data, dict) or data.get('code') not in ('instance_changed', 'cursor_ahead') or
                        not instance(data.get('instance_id'))):
                    raise ProtocolFailure('invalid replay conflict') from exc
                raise ReplayConflict(data['code'], data['instance_id']) from exc
            if exc.code == 503 and path == "/api/stats":
                raise NoCurrentData("no current stats") from exc
            if exc.code == 404 and path == "/api/observations":
                raise OptionalUnsupported("optional endpoint not provided") from exc
            if exc.code in (301, 302, 303, 307, 308):
                raise ProtocolFailure("redirect disallowed") from exc
            raise ServerFailure(f"HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError, OSError) as exc:
            raise TransportFailure("connection/timeout error") from exc
        return decode_json(content)
