import datetime
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from framebridge_expanded import connection_monitor as monitor
from framebridge_expanded import tunnel

ID = "tunnel_synthetic_connection_test"
MCP = "http://127.0.0.1:20865/mcp"


class HTTPFixture:
    def __init__(self, root):
        self.started = time.time()
        self.ready = "ready"
        self.ready_code = 200
        self.metadata_error = ""
        self.poll = self.started
        self.tunnel = ID
        self.mcp = MCP
        self.hits = []
        fixture = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.hits.append(self.path)
                if self.path == "/readyz":
                    code, data = fixture.ready_code, fixture.ready.encode()
                elif self.path == "/api/status":
                    code = 200
                    data = json.dumps({"control_plane_tunnel_id": fixture.tunnel,
                                       "mcp_server_url": fixture.mcp,
                                       "started_at": datetime.datetime.fromtimestamp(fixture.started, datetime.timezone.utc).isoformat(),
                                       "tunnel_metadata": {"ID": fixture.tunnel},
                                       "tunnel_metadata_error": fixture.metadata_error}).encode()
                else:
                    code, data = 200, ("commands_poll_last_successful_timestamp_seconds " + str(fixture.poll) + "\n").encode()
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "http://192.0.2.1/forbidden")
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.file = Path(root) / "health.url"
        self.file.write_text("http://127.0.0.1:" + str(self.server.server_port))

    def probe(self):
        return monitor.probe(self.file, ID, MCP, self.started)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fb-health-test-")
        self.fixture = HTTPFixture(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.fixture.close)

    def test_ready_requires_local_mcp_identity_and_authenticated_poll(self):
        self.assertEqual(self.fixture.probe()[0], "ready")
        self.assertEqual(self.fixture.hits, ["/readyz", "/api/status", "/metrics"])

    def test_no_success_for_live_client_without_successful_poll(self):
        self.fixture.poll = 0
        self.assertEqual(self.fixture.probe()[0], "access")

    def test_no_success_for_stale_or_future_poll(self):
        self.fixture.poll = self.fixture.started - 200
        self.assertEqual(self.fixture.probe()[0], "access")
        self.fixture.poll = self.fixture.started + 200
        self.assertEqual(self.fixture.probe()[0], "access")

    def test_partial_ready_auth_and_timeout_are_not_full_ready(self):
        for suffix in [" (mcp initialize requires auth: synthetic)", " (mcp startup probe timed out: synthetic)"]:
            self.fixture.ready = "ready" + suffix
            self.assertEqual(self.fixture.probe()[0], "checking")
        self.assertNotIn("/metrics", self.fixture.hits)

    def test_changed_identity_or_old_startup_not_accepted(self):
        self.fixture.tunnel = "tunnel_other"
        self.assertEqual(self.fixture.probe()[0], "checking")
        self.fixture.tunnel = ID
        self.assertEqual(monitor.probe(self.fixture.file, ID, MCP, self.fixture.started + 50)[0], "checking")

    def test_metadata_error_is_not_connected(self):
        self.fixture.metadata_error = "HTTP 403 synthetic fixture"
        self.assertEqual(self.fixture.probe()[0], "rejected")

    def test_redirects_and_proxy_environment_do_not_redirect_probe(self):
        self.fixture.ready_code = 302
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://192.0.2.2:1", "NO_PROXY": ""}):
            self.assertEqual(self.fixture.probe()[0], "checking")
        self.assertEqual(self.fixture.hits, ["/readyz"])

    def test_health_file_rejects_remote_credentials_query_and_symlink(self):
        for value in ["http://192.0.2.1:8", "http://user:pw@127.0.0.1:8", "http://127.0.0.1:8/?token=x", "http://localhost:8", "https://127.0.0.1:8"]:
            self.fixture.file.write_text(value)
            with self.assertRaises(monitor.ConnectionMonitorError):
                monitor.health_endpoint(self.fixture.file)
        self.fixture.file.unlink()
        self.fixture.file.symlink_to(Path(self.temp.name) / "missing")
        with self.assertRaises(OSError):
            monitor.health_endpoint(self.fixture.file)


class PrivateLogTests(unittest.TestCase):
    def test_rotation_is_bounded_and_preserves_only_redacted_private_records(self):
        with tempfile.TemporaryDirectory() as td:
            log = monitor.PrivateLog(td, "SYNTHETIC_SECRET", max_bytes=220)
            for _ in range(20):
                log.write("event SYNTHETIC_SECRET " + "x" * 55)
            log.close()
            files = list((Path(td) / "logs/chatgpt").glob("connection.log*"))
            self.assertLessEqual(len(files), 3)
            for p in files:
                self.assertLessEqual(p.stat().st_size, 220)
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("SYNTHETIC_SECRET", p.read_text())

    def test_no_symlink_hardlink_or_permissive_log_is_written(self):
        for kind in ["symlink", "hardlink", "permissions"]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                log = monitor.PrivateLog(td, "fixture")
                p = log.path
                target = Path(td) / "untouched"
                target.write_text("unchanged")
                target.chmod(0o600)
                if kind == "symlink":
                    p.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, p)
                else:
                    p.write_text("unchanged")
                    p.chmod(0o644)
                with self.assertRaises((OSError, monitor.ConnectionMonitorError)):
                    log.write("must not be stored")
                self.assertEqual(target.read_text(), "unchanged")
                log.close()

    def test_fifo_paths_are_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as td:
            log = monitor.PrivateLog(td, "fixture")
            os.mkfifo(log.path, 0o600)
            with self.assertRaises((OSError, monitor.ConnectionMonitorError)):
                log.write("must not block")
            log.close()
            endpoint = Path(td) / "health.url"
            os.mkfifo(endpoint, 0o600)
            with self.assertRaises((OSError, monitor.ConnectionMonitorError)):
                monitor.health_endpoint(endpoint)

    def test_capture_redacts_secret_split_across_reads_and_drops_oversize_line(self):
        with tempfile.TemporaryDirectory() as td:
            secret = "FAKE_CROSS_CHUNK_CREDENTIAL"
            log = monitor.PrivateLog(td, secret)
            read_fd, write_fd = os.pipe()
            errors, stop = [], threading.Event()
            stream = os.fdopen(read_fd, "rb", buffering=0)
            worker = threading.Thread(target=monitor.capture_output, args=(stream, log, errors, stop))
            worker.start()
            for part in [b"debug " + secret[:9].encode(), secret[9:].encode() + b"\n", b"A" * (monitor.MAX_LINE + 100), b"\n"]:
                os.write(write_fd, part)
            os.close(write_fd)
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            data = log.path.read_text()
            self.assertNotIn(secret, data)
            self.assertIn("debug [REDACTED]", data)
            self.assertIn("Oversized client output line omitted", data)
            stream.close()
            log.close()

    def test_secret_headers_and_quoted_values_are_not_partially_persisted(self):
        for text in ['Cookie: first=abc; second=def', 'Authorization: Bearer abc xyz',
                     '\"password\": \"secret with spaces\"', 'url?access_token=abc&next=def']:
            clean = monitor.redact(text, "fixture")
            for fragment in ["abc", "def", "secret with spaces", "first=", "second="]:
                self.assertNotIn(fragment, clean)
            self.assertIn("[REDACTED]", clean)

    def test_normalization_cannot_reconstruct_a_secret_after_redaction(self):
        secret = "FAKE_CREDENTIAL"
        for middle in ["\x1b[31m", "\x01", "\u200b"]:
            value = "FAKE_" + middle + "CREDENTIAL"
            clean = monitor.redact(value, secret)
            self.assertNotIn(secret, clean)
            self.assertNotIn("CREDENTIAL", clean)
            self.assertIn("[REDACTED]", clean)

    def test_proxy_url_userinfo_is_redacted(self):
        for value in ["proxy http://user:FAKE_PASSWORD@proxy.example:80/path", "https://name:p%40ss@host.example/next"]:
            clean = monitor.redact(value, "different_runtime_fixture")
            for fragment in ["user:", "FAKE_PASSWORD", "name:", "p%40ss"]:
                self.assertNotIn(fragment, clean)
            self.assertIn("[REDACTED]@", clean)

    def test_non_tty_progress_does_not_animate_or_repeat_same_state(self):
        out = io.StringIO()
        progress = monitor.Progress(out)
        for _ in range(8):
            progress.change("checking", "Checking connection")
            progress.tick()
        self.assertEqual(out.getvalue(), "Checking connection\n")


class ForegroundFailureTests(unittest.TestCase):
    def command(self, code):
        return [sys.executable, "-u", "-c", code,
                "--control-plane.tunnel-id", ID, "--mcp.server-url", "url=" + MCP]

    def test_early_failure_has_clear_error_and_no_technical_terminal_dump(self):
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            result = tunnel.run_foreground(self.command("import sys;print('TECHNICAL_PRIVATE_DETAIL');sys.exit(3)"),
                                           "SYNTHETIC_KEY", state=td, output=out)
            self.assertEqual(result, 3)
            self.assertIn("[ERROR]", out.getvalue())
            self.assertNotIn("[OK]", out.getvalue())
            self.assertNotIn("TECHNICAL_PRIVATE_DETAIL", out.getvalue())
            self.assertIn("TECHNICAL_PRIVATE_DETAIL", (Path(td) / "logs/chatgpt/connection.log").read_text())

    def test_exit_before_verified_state_does_not_report_success(self):
        seen = {}
        def spawn(*args, **kwargs):
            seen["child"] = subprocess.Popen(*args, **kwargs)
            return seen["child"]
        def probe(*args):
            seen["child"].wait(timeout=3)
            return "ready", "synthetic readiness arrived after exit"
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with mock.patch.object(monitor, "probe", side_effect=probe):
                result = tunnel.run_foreground(self.command("pass"), "SYNTHETIC_KEY", state=td, output=out, popen=spawn)
            self.assertEqual(result, 1)
            self.assertNotIn("[OK]", out.getvalue())

    def test_cleanup_failure_does_not_skip_signal_restoration(self):
        import signal
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with mock.patch.object(tunnel, "_stop_owned", side_effect=OSError("synthetic cleanup failure")):
                rc = tunnel.run_foreground(self.command("pass"), "SYNTHETIC_KEY", state=td, output=out)
            self.assertEqual(rc, 1)
            self.assertIn("cleanup could not be fully verified", out.getvalue())
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_verified_connection_loss_is_explicit_and_ctrl_c_still_cleans_up(self):
        states = iter([("ready", "synthetic verified"), ("access", "synthetic poll lost")])
        def probe(*args):
            try:
                return next(states)
            except StopIteration:
                raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with mock.patch.object(monitor, "probe", side_effect=probe):
                result = tunnel.run_foreground(self.command("import time;time.sleep(60)"), "SYNTHETIC_KEY", state=td, output=out)
            self.assertEqual(result, 130)
            self.assertIn("[RETRYING]", out.getvalue())
            self.assertIn("[STOPPED]", out.getvalue())
            self.assertNotIn("Traceback", out.getvalue())
