"""Operator-managed, immutable-in-process node registry."""

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


# Raw capture level per node (ADR-0007); only meaningful with --raw-capture.
CAPTURE_LEVELS = ("auto", "replay", "sampled", "off")


@dataclass(frozen=True)
class Node:
    node_id: str
    name: str
    endpoint: str
    enabled: bool = True
    capture: str = "auto"


def node_record(node):
    """Registry JSON for a node; the default capture level is omitted so files stay readable by older Hubs."""
    record = asdict(node)
    if record["capture"] == "auto":
        del record["capture"]
    return record


def load_registry(path):
    """Read explicit node inventory; no scanning or remote registration."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_registry(raw)


def validate_registry(raw):
    """Validate without writing; shared by loader and local CLI."""
    if not isinstance(raw, dict) or set(raw) != {"nodes"} or not isinstance(raw["nodes"], list):
        raise ValueError("registry must be an object with one nodes list")
    nodes, ids, endpoints = [], set(), set()
    if len(raw["nodes"]) > 256:
        raise ValueError("registry exceeds 256 nodes")
    for record in raw["nodes"]:
        if not isinstance(record, dict) or not {"node_id", "name", "endpoint"} <= set(record) or set(record) - {"node_id", "name", "endpoint", "enabled", "capture"}:
            raise ValueError("invalid node record")
        identity, name, endpoint = record["node_id"], record["name"], record["endpoint"]
        enabled = record.get("enabled", True)
        capture = record.get("capture", "auto")
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
            raise ValueError("node_id must be canonical UUID")
        if not isinstance(name, str) or not (1 <= len(name) <= 100) or not name.strip():
            raise ValueError("node name must be nonempty and at most 100 characters")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if capture not in CAPTURE_LEVELS:
            raise ValueError("capture must be one of auto, replay, sampled or off")
        if not isinstance(endpoint, str):
            raise ValueError("endpoint must be an HTTP origin")
        url = urlsplit(endpoint)
        try:
            hostname = url.hostname
            port = url.port if url.port is not None else 80
        except ValueError as exc:
            raise ValueError("invalid agent port") from exc
        if (url.scheme != "http" or not hostname or url.username is not None or
                url.password is not None or url.path not in ("", "/") or url.query or
                url.fragment or not 1 <= port <= 65535 or any(c.isspace() for c in endpoint)):
            raise ValueError("agent endpoint must be a plain HTTP origin without credentials or path")
        origin = f"http://{hostname.lower()}:{port}"
        if identity in ids or origin in endpoints:
            raise ValueError("duplicate node_id or agent endpoint")
        ids.add(identity)
        endpoints.add(origin)
        nodes.append(Node(identity, name, endpoint.rstrip("/"), enabled, capture))
    return tuple(nodes)
