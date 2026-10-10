"""Hub port selection and bind failure must be safe and explicit."""

import argparse
import errno
import unittest
from unittest import mock

from hub.__main__ import bind_hub, main, tcp_port


class PortPolicyTests(unittest.TestCase):
    def test_cli_rejects_ephemeral_and_invalid_ports(self):
        for value in ("0", "-1", "65536", "abc", "9787.1"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    tcp_port(value)
        self.assertEqual(tcp_port("9787"), 9787)
        self.assertEqual(tcp_port("9798"), 9798)

    def test_existing_listener_reports_requested_port_without_fallback(self):
        fleet = mock.Mock()
        with mock.patch("hub.__main__.HubServer",
                        side_effect=OSError(errno.EADDRINUSE, "address already in use")) as server:
            with self.assertRaises(SystemExit) as cm:
                bind_hub("127.0.0.1", 9787, fleet)
        self.assertIn("127.0.0.1:9787", str(cm.exception))
        self.assertIn("already in use", str(cm.exception))
        self.assertIn("--port", str(cm.exception))
        server.assert_called_once_with(("127.0.0.1", 9787), fleet)

    def test_windows_permission_or_reservation_error(self):
        fleet = mock.Mock()
        with mock.patch("hub.__main__.HubServer",
                        side_effect=OSError(10013, "forbidden")):
            with self.assertRaises(SystemExit) as cm:
                bind_hub("127.0.0.1", 9798, fleet)
        self.assertIn("permission denied or port reserved", str(cm.exception))
        self.assertIn("9798", str(cm.exception))

    def test_unrecognized_socket_error_is_not_masked(self):
        with mock.patch("hub.__main__.HubServer",
                        side_effect=OSError(errno.EADDRNOTAVAIL, "not available")):
            with self.assertRaises(OSError) as cm:
                bind_hub("203.0.113.9", 9787, mock.Mock())
        self.assertEqual(cm.exception.errno, errno.EADDRNOTAVAIL)

    def test_failed_startup_closes_fleet(self):
        fleet = mock.Mock()
        with mock.patch("hub.__main__.load_registry", return_value=[]), \
             mock.patch("hub.__main__.Fleet", return_value=fleet), \
             mock.patch("hub.__main__.HubServer", side_effect=OSError(errno.EADDRINUSE, "busy")):
            with self.assertRaises(SystemExit):
                main(["--nodes", "not-read.json"])
        fleet.start.assert_not_called()
        fleet.close.assert_called_once()

    def test_cli_refuses_auto_assigned_port(self):
        with self.assertRaises(SystemExit) as cm, mock.patch("hub.__main__.load_registry") as registry:
            main(["--nodes", "nodes.json", "--port", "0"])
        self.assertEqual(cm.exception.code, 2)
        registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
