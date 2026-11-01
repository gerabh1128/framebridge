"""Exercise the actual broker server transport with real loopback TLS.

The negative control uses stdlib ThreadingHTTPServer with its old automatic
listener handshake, reproducing the pre-fix design without a duplicate broker.
Application profile and authentication tests remain in test_fleet_broker_node.
"""
import ast
import concurrent.futures
import contextlib
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


CANDIDATE = Path(__file__).resolve().parents[1] / "expanded/framebridge_expanded/broker.py"


def candidate_class(host):
    tree = ast.parse(CANDIDATE.read_text())
    factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_server")
    cls = next(n for n in factory.body if isinstance(n, ast.ClassDef) and n.name == "Server")
    namespace = dict(host=host, socket=socket, ssl=ssl, threading=threading,
                     ThreadingHTTPServer=ThreadingHTTPServer)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(CANDIDATE), "exec"), namespace)
    return namespace["Server"]


def until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return bool(predicate())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self.server.handled.append(self.path)
        if self.path == "/long-poll":
            time.sleep(.4)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


class TLSRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cert = str(Path(cls.tmp.name) / "cert.pem")
        cls.key = str(Path(cls.tmp.name) / "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "1", "-keyout", cls.key, "-out", cls.cert,
                        "-subj", "/CN=localhost", "-addext",
                        "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.client_context = ssl.create_default_context(cafile=cls.cert)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @contextlib.contextmanager
    def running(self, *, tls=True, original=False, host="127.0.0.1", limit=128, timeout=.25):
        cls = ThreadingHTTPServer if original else candidate_class(host)
        if not original:
            cls.max_workers = limit
            cls.tls_handshake_timeout = timeout
        server = cls((host, 0), Handler)
        server.daemon_threads = True
        server.handled = []
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.cert, self.key)
            server.socket = context.wrap_socket(server.socket, server_side=True,
                                               do_handshake_on_connect=original)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "accept loop did not stop")

    def connect(self, server, timeout=1):
        return socket.create_connection(server.server_address[:2], timeout=timeout)

    def fetch(self, server, path="/health", *, tls=True, timeout=1):
        with self.connect(server, timeout) as raw:
            stream = self.client_context.wrap_socket(raw, server_hostname="localhost") if tls else raw
            with stream:
                stream.sendall(("GET " + path + " HTTP/1.0\r\nHost: localhost\r\n\r\n").encode())
                response = b""
                while True:
                    part = stream.recv(4096)
                    if not part:
                        break
                    response += part
        self.assertIn(b"200 OK", response)
        self.assertTrue(response.endswith(b"OK"))
        return response

    def test_original_control_is_blocked_by_one_unfinished_handshake(self):
        with self.running(original=True) as server:
            raw = self.connect(server)
            try:
                # Wait until accept removed the TCP connection from the listen queue.
                time.sleep(.05)
                with self.assertRaises(TimeoutError):
                    self.fetch(server, timeout=.15)
            finally:
                raw.close()
            self.fetch(server)

    def test_https_healthy_while_raw_tcp_and_partial_tls_stall(self):
        with self.running(timeout=1) as server:
            with self.connect(server) as raw, self.connect(server) as partial:
                partial.sendall(b"\x16\x03\x01\x00\xff\x01")
                self.assertTrue(until(lambda: server._worker_slots._value <= 126))
                start = time.monotonic()
                self.fetch(server, timeout=.5)
                self.assertLess(time.monotonic() - start, .5)
                self.assertGreaterEqual(server._worker_slots._value, 125)

    def test_stalled_handshake_times_out_and_releases_capacity(self):
        with self.running(timeout=.15, limit=2) as server:
            with self.connect(server) as raw:
                self.assertTrue(until(lambda: server._worker_slots._value == 1))
                raw.settimeout(1)
                self.assertEqual(raw.recv(1), b"")
                self.assertTrue(until(lambda: server._worker_slots._value == 2))
                self.assertEqual(server.handled, [])
            self.fetch(server)

    def test_plaintext_on_tls_is_rejected_without_handler_or_slot_leak(self):
        with self.running(limit=2) as server:
            with self.connect(server) as raw:
                raw.sendall(b"GET /bad HTTP/1.0\r\n\r\n")
                raw.settimeout(1)
                try:
                    self.assertEqual(raw.recv(1024), b"")
                except ConnectionResetError:
                    pass
            self.assertTrue(until(lambda: server._worker_slots._value == 2))
            self.assertEqual(server.handled, [])
            self.fetch(server)

    def test_https_concurrent_requests(self):
        with self.running() as server:
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                # Keep the original listen backlog (5). A 12-client TCP burst can
                # legitimately need one SYN retry even when accept is responsive.
                futures = [pool.submit(self.fetch, server, "/health", timeout=3) for _ in range(36)]
                for result in futures:
                    result.result(timeout=5)
            self.assertEqual(len(server.handled), 36)
            self.assertTrue(until(lambda: server._worker_slots._value == 128))

    def test_long_poll_is_not_cut_off_by_handshake_timeout(self):
        with self.running(timeout=.1) as server:
            self.fetch(server, "/long-poll", timeout=1)
            self.assertEqual(server.handled, ["/long-poll"])

    def test_capacity_rejects_promptly_and_recovers(self):
        with self.running(limit=2, timeout=2) as server:
            first, second = self.connect(server), self.connect(server)
            try:
                self.assertTrue(until(lambda: server._worker_slots._value == 0))
                start = time.monotonic()
                with self.connect(server) as excess:
                    excess.settimeout(.5)
                    self.assertEqual(excess.recv(1), b"")
                self.assertLess(time.monotonic() - start, .5)
                self.assertEqual(server._worker_slots._value, 0)
            finally:
                first.close()
                second.close()
            self.assertTrue(until(lambda: server._worker_slots._value == 2))
            self.fetch(server)

    def test_worker_start_failure_releases_capacity(self):
        cls = candidate_class("127.0.0.1")
        cls.max_workers = 2
        server = cls(("127.0.0.1", 0), Handler)
        left, right = socket.socketpair()
        try:
            with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread limit")):
                with self.assertRaises(RuntimeError):
                    server.process_request(left, ("127.0.0.1", 0))
            self.assertEqual(server._worker_slots._value, 2)
        finally:
            left.close()
            right.close()
            server.server_close()

    def test_plain_http_remains_supported(self):
        with self.running(tls=False) as server:
            self.fetch(server, tls=False)
            self.fetch(server, "/long-poll", tls=False)
            self.assertTrue(until(lambda: server._worker_slots._value == 128))

    @unittest.skipUnless(socket.has_ipv6, "IPv6 unavailable")
    def test_ipv6_https_remains_supported(self):
        try:
            with self.running(host="::1") as server:
                self.fetch(server)
        except OSError as error:
            if error.errno in (97, 99):
                self.skipTest("IPv6 loopback unavailable")
            raise

    def test_shutdown_accept_loop_with_stalled_connection(self):
        with self.running(timeout=.15) as server:
            with self.connect(server) as raw:
                self.assertTrue(until(lambda: server._worker_slots._value == 127))
                server.shutdown()
                raw.settimeout(1)
                self.assertEqual(raw.recv(1), b"")
                self.assertTrue(until(lambda: server._worker_slots._value == 128))

    def test_candidate_keeps_tls12_minimum_and_defers_handshake(self):
        tree = ast.parse(CANDIDATE.read_text())
        factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_server")
        wraps = [n for n in ast.walk(factory) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "wrap_socket"]
        self.assertEqual(len(wraps), 1)
        self.assertTrue(any(k.arg == "do_handshake_on_connect" and isinstance(k.value, ast.Constant)
                            and k.value.value is False for k in wraps[0].keywords))
        assignments = [n for n in ast.walk(factory) if isinstance(n, ast.Assign)]
        self.assertTrue(any(any(isinstance(t, ast.Attribute) and t.attr == "minimum_version"
                                for t in n.targets) and ast.unparse(n.value) == "ssl.TLSVersion.TLSv1_2"
                            for n in assignments))


if __name__ == "__main__":
    unittest.main(verbosity=2)
