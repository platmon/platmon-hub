"""Run the bounded read-only fleet service (trusted management network only)."""

import argparse
import logging
from .http import HubServer
from .registry import load_registry
from .runtime import Fleet


def main(argv=None):
    parser = argparse.ArgumentParser(description="platmon-hub read-only backend")
    parser.add_argument("--nodes", required=True, help="path to explicitly managed node registry JSON")
    parser.add_argument("--bind", default="127.0.0.1", help="Hub bind address; loopback by default")
    parser.add_argument("--port", type=int, default=9787)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    fleet = Fleet(load_registry(args.nodes))
    server = HubServer((args.bind, args.port), fleet)
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
