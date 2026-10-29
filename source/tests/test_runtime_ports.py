"""Exercise the real start preflight with disposable TCP sockets, without spawning services."""
import errno
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from framebridge_expanded import runtime


class SpawnBoundaryReached(Exception):
    pass


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux TCP restart regression")
class RuntimePortPreflightTests(unittest.TestCase):
    def start_until_spawn(self, state, ports):
        settings = {"role": "central", "ports": ports}
        with patch.object(runtime, "state_path", return_value=state), \
             patch.object(runtime, "load_config", return_value=settings), \
             patch.object(runtime, "status", return_value={"running": False}), \
             patch.object(runtime, "doctor", return_value={"ok": True}), \
             patch.object(runtime, "environment", return_value={}), \
             patch.object(runtime, "_spawn", side_effect=SpawnBoundaryReached) as spawn:
            with self.assertRaises(SpawnBoundaryReached):
                runtime.start(state)
            spawn.assert_called_once()

    def close_server_first(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.settimeout(2)
            client.settimeout(2)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            client.connect(("127.0.0.1", port))
            with listener.accept()[0] as accepted:
                accepted.settimeout(2)
                accepted.shutdown(socket.SHUT_WR)
                self.assertEqual(client.recv(1), b"")
                client.close()
                self.assertEqual(accepted.recv(1), b"")
        return port

    def test_closed_connections_do_not_block_central_restart(self):
        ports = {name: self.close_server_first() for name in ("mcp", "hub", "broker")}
        self.assertEqual(len(set(ports.values())), 3)
        # Establish that each port has the regression condition, not an active listener.
        for port in ports.values():
            with socket.socket() as original_probe:
                with self.assertRaises(OSError) as blocked:
                    original_probe.bind(("127.0.0.1", port))
                self.assertEqual(blocked.exception.errno, errno.EADDRINUSE)
        with tempfile.TemporaryDirectory() as directory:
            self.start_until_spawn(Path(directory), ports)

    def test_active_loopback_and_wildcard_listeners_still_block_start(self):
        for address in ("127.0.0.1", "0.0.0.0"):
            with self.subTest(address=address), socket.socket() as listener, \
                 tempfile.TemporaryDirectory() as directory:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((address, 0))
                listener.listen(1)
                port = listener.getsockname()[1]
                # The first preflight port is occupied, so no process may be spawned.
                ports = {name: port for name in ("mcp", "hub", "broker")}
                with self.assertRaises(OSError) as blocked:
                    self.start_until_spawn(Path(directory), ports)
                self.assertEqual(blocked.exception.errno, errno.EADDRINUSE)


if __name__ == "__main__":
    unittest.main()
