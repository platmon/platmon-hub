"""Run the bounded read-only fleet service (trusted management network only)."""

import argparse
import errno
import logging
import math
import sys
from .http import HubServer
from .registry import load_registry
from .runtime import Fleet
from .history.writer import HistoryWriter


def tcp_port(value):
    """Require a fixed, explicit TCP port; 0 would request an ephemeral port."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer (1-65535)") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in the range 1-65535 (0 is not allowed)")
    return port


def bind_hub(bind, port, fleet):
    """Fail closed and explain local bind conflicts; never select another port."""
    try:
        return HubServer((bind, port), fleet)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 10048):
            reason = "address/port is already in use"
        elif exc.errno in (errno.EACCES, errno.EPERM, 10013):
            reason = "permission denied or port reserved by the OS"
        else:
            raise
        raise SystemExit(
            f"platmon-hub: cannot listen on {bind}:{port}: {reason}. "
            "Check the local listener and OS port reservations; "
            "stop the conflicting service or set an explicit --port. "
            "No automatic port fallback is attempted."
        ) from None


def positive_days(value):
    try:
        days = float(value)
        if not math.isfinite(days) or days <= 0:
            raise ValueError()
        return days
    except ValueError as exc:
        raise argparse.ArgumentTypeError("retention days must be finite and positive") from exc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "nodes":
        from .nodes_cli import main as nodes_main
        return nodes_main(argv[1:])
    parser = argparse.ArgumentParser(description="platmon-hub read-only backend")
    parser.add_argument("--nodes", required=True, help="path to explicitly managed node registry JSON")
    parser.add_argument("--bind", default="127.0.0.1", help="Hub bind address; loopback by default")
    parser.add_argument("--port", type=tcp_port, default=9787, help="Hub TCP listen port (default: 9787)")
    parser.add_argument("--history-db", help="explicit local SQLite history path (disabled by default)")
    parser.add_argument("--history-retention-days", type=positive_days, default=7)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    nodes = load_registry(args.nodes)
    history = HistoryWriter(args.history_db, retention_days=args.history_retention_days) if args.history_db else None
    fleet = Fleet(nodes, history=history)
    try:
        server = bind_hub(args.bind, args.port, fleet)
    except BaseException:
        # A failed bind must not strand an unstarted thread pool.
        fleet.close()
        raise
    try:
        fleet.start()
        print(f"platmon-hub serving on {server.server_address[0]}:{server.server_address[1]}", flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        fleet.close()


if __name__ == "__main__":
    main()
