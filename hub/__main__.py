"""Run the bounded read-only fleet service (trusted management network only)."""

import argparse
import errno
import logging
import math
import sys
import signal
from logging.handlers import RotatingFileHandler
from . import __version__
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


def bind_hub(bind, port, fleet, admin=None):
    """Fail closed and explain local bind conflicts; never select another port."""
    try:
        if admin is None:
            return HubServer((bind, port), fleet)
        return HubServer((bind, port), fleet, admin=admin)
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
    if argv and argv[0] == "archive":
        from .archive import main as archive_main
        return archive_main(argv[1:])
    if argv and argv[0] == "export":
        from .export import main as export_main
        return export_main(argv[1:])
    if argv and argv[0] == "backup":
        from .backup import main as backup_main
        return backup_main(argv[1:])
    if argv and argv[0] == "nodes":
        from .nodes_cli import main as nodes_main
        return nodes_main(argv[1:])
    parser = argparse.ArgumentParser(description="platmon-hub read-only backend")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--log-file", help="optional UTF-8 rotating local log file")
    parser.add_argument("--nodes", required=True, help="path to explicitly managed node registry JSON")
    parser.add_argument("--bind", default="127.0.0.1", help="Hub bind address; loopback by default")
    parser.add_argument("--port", type=tcp_port, default=9787, help="Hub TCP listen port (default: 9787)")
    parser.add_argument("--web-admin", action="store_true", help="enable device management for trusted local users (loopback only)")
    parser.add_argument("--alerts", help="local alert rules JSON (requires history)")
    parser.add_argument("--history-db", help="explicit local SQLite history path (disabled by default)")
    parser.add_argument("--archive-dir", help="opt-in dedicated daily CSV ZIP archive directory (requires history)")
    parser.add_argument("--archive-retention-days", type=int, default=90)
    parser.add_argument("--archive-quota-mib", type=int, default=10240)
    parser.add_argument("--event-retention-days", type=positive_days, default=30)
    parser.add_argument("--history-retention-days", type=positive_days, default=7)
    parser.add_argument("--no-registry-reload", action="store_true", help="disable automatic registry reload")
    args = parser.parse_args(argv)
    if args.archive_dir and (not args.history_db or not 1 <= args.archive_retention_days <= 3650 or not 1 <= args.archive_quota_mib <= 1048576):
        parser.error("--archive-dir requires history and valid retention/quota bounds")
    archive = None
    admin = None
    if args.web_admin:
        if args.bind != '127.0.0.1' or args.no_registry_reload:
            parser.error('--web-admin requires --bind 127.0.0.1 and registry reload')
        from .admin import RegistryAdmin
        admin = RegistryAdmin(args.nodes)
    handlers = [RotatingFileHandler(args.log_file, maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")] if args.log_file else None
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    nodes = load_registry(args.nodes)
    from .alerts import load_rules
    if args.alerts and not args.history_db:
        parser.error('--alerts requires --history-db')
    try:
        alert_rules = load_rules(args.alerts) if args.alerts else None
    except (ValueError, OSError):
        parser.error('invalid or unreadable alert configuration')
    history = HistoryWriter(args.history_db, retention_days=args.history_retention_days, event_retention_days=args.event_retention_days) if args.history_db else None
    fleet = Fleet(nodes, history=history, registry_path=None if args.no_registry_reload else args.nodes, alert_rules=alert_rules)
    try:
        server = bind_hub(args.bind, args.port, fleet, admin=admin)
    except BaseException:
        # A failed bind must not strand an unstarted thread pool.
        fleet.close()
        raise
    if args.archive_dir:
        from .archive import ArchiveScheduler
        archive = ArchiveScheduler(args.history_db, args.archive_dir, args.archive_retention_days, args.archive_quota_mib * 1024 ** 2, min(31, max(1, math.ceil(args.history_retention_days))))
    server.archive = archive
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        fleet.start()
        if archive is not None: archive.start()
        logging.info("platmon-hub serving on %s:%s", *server.server_address[:2])
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if archive is not None: archive.close()
        fleet.close()
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
