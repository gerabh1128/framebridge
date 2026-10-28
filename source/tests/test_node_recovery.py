"""Socket fault injection against the real FBPAIR broker and enrolled identities."""
import errno
import hashlib
import selectors
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
from unittest import mock

from framebridge_expanded import broker, node
import test_fleet_broker_node as fixtures


class StopLoop(BaseException):
    pass


class NodeRecoveryTests(unittest.TestCase):
    setUp = fixtures.BrokerNodeTests.setUp
    tearDown = fixtures.BrokerNodeTests.tearDown
    make_node = fixtures.BrokerNodeTests.make_node
    pair = fixtures.BrokerNodeTests.pair

    @staticmethod
    def disconnect(handler):
        handler.close_connection = True
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()

    def test_transient_poll_disconnects_backoff_cap_and_inventory_recovery(self):
        state, ident = self.pair("recover-node")
        node.run(state, once=True)
        store = self.server.expanded_store
        store.node_last_seen[ident] = time.time() - 10
        self.assertFalse(broker.list_confirmed_nodes()[0]["ready"])
        identity_before = (state / "node/identity.json").read_bytes()
        original = self.server.RequestHandlerClass.do_POST
        real_post = broker.post_json
        attempts, waits, envelopes = [], [], []
        recovered = []

        def fault(handler):
            if handler.path.endswith("/node/poll"):
                attempts.append(1)
                if len(attempts) <= 8:
                    self.disconnect(handler)
                    return
            return original(handler)

        def post(url, envelope, **kwargs):
            if url.endswith("/node/poll"):
                envelopes.append(envelope)
            result = real_post(url, envelope, **kwargs)
            if url.endswith("/node/poll"):
                recovered.append(True)
            return result

        def pause(seconds):
            if recovered:
                raise StopLoop
            self.assertFalse(store.node_runtime_status(ident)["ready"])
            waits.append(seconds)

        with mock.patch.object(self.server.RequestHandlerClass, "do_POST", fault), \
                mock.patch.object(broker, "post_json", side_effect=post), \
                mock.patch.object(node, "time", wraps=time) as clock, \
                self.assertLogs(node._LOG, level="WARNING") as logs:
            clock.sleep.side_effect = pause
            with self.assertRaises(StopLoop):
                node.run(state)
        self.assertEqual(waits, [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(len(attempts), 9)
        self.assertEqual(len({e["nonce"] for e in envelopes}), 9)
        self.assertTrue(store.node_runtime_status(ident)["ready"])
        self.assertEqual((state / "node/identity.json").read_bytes(), identity_before)
        self.assertTrue(any("NODE_BROKER_RECOVERED" in line for line in logs.output))
        self.assertTrue(all("signature" not in line and "payload" not in line for line in logs.output))

    def test_result_disconnect_before_and_after_acceptance_never_reexecutes(self):
        state, ident = self.pair("ack-node")
        marker = self.root / "executed.txt"
        real_post = broker.post_json
        original_post = self.server.RequestHandlerClass.do_POST
        original_send = self.server.RequestHandlerClass.send_json
        for after_acceptance in (False, True):
            with self.subTest(after_acceptance=after_acceptance):
                marker.write_text("")
                script = "printf x >> " + shlex.quote(str(marker))
                payload = {"authorized": True, "job_id": "recovery-command", "timeout_s": 10,
                           "script": script, "sha256": hashlib.sha256(script.encode()).hexdigest()}
                queued, error = self.server.expanded_store.queue_admin(ident, "admin.exec", payload)
                self.assertIsNone(error)
                posted, dispatched, waits, dropped, delivered = [], [], [], [], []

                def execute(value):
                    dispatched.append(value["job_id"])
                    subprocess.run(["/bin/sh", "-c", value["script"]], check=True)
                    return {"ok": True, "exit_code": "0"}

                def fault_post(handler):
                    if not after_acceptance and handler.path.endswith("/node/result") and not dropped:
                        dropped.append(True)
                        self.disconnect(handler)
                        return
                    return original_post(handler)

                def fault_send(handler, code, value):
                    if after_acceptance and handler.path.endswith("/node/result") and not dropped:
                        dropped.append(True)
                        self.disconnect(handler)
                        return
                    return original_send(handler, code, value)

                def post(url, envelope, **kwargs):
                    if url.endswith("/node/result"):
                        posted.append(envelope)
                    result = real_post(url, envelope, **kwargs)
                    if url.endswith("/node/result"):
                        delivered.append(True)
                    return result

                def pause(seconds):
                    if delivered:
                        raise StopLoop
                    waits.append(seconds)

                with mock.patch.object(self.server.RequestHandlerClass, "do_POST", fault_post), \
                        mock.patch.object(self.server.RequestHandlerClass, "send_json", fault_send), \
                        mock.patch.object(broker, "post_json", side_effect=post), \
                        mock.patch("framebridge_expanded.runtime.adapter_request", side_effect=execute), \
                        mock.patch.object(node, "time", wraps=time) as clock, \
                        self.assertLogs(node._LOG, level="WARNING"):
                    clock.sleep.side_effect = pause
                    with self.assertRaises(StopLoop):
                        node.run(state)
                self.assertEqual(marker.read_text(), "x")
                self.assertEqual(dispatched, ["recovery-command"])
                self.assertEqual(waits, [1])
                self.assertEqual(len(posted), 2)
                self.assertNotEqual(posted[0]["nonce"], posted[1]["nonce"])
                self.assertEqual(posted[0]["payload"], posted[1]["payload"])
                saved = self.server.expanded_store.get_result(queued["request_id"])
                self.assertEqual(saved["state"], "complete")
                self.assertTrue(saved["result"]["ok"])

    def test_broker_restart_after_execution_is_terminal_without_replay(self):
        state, ident = self.pair("orphan-node")
        marker = self.root / "orphan-effect.txt"
        script = "printf x >> " + shlex.quote(str(marker))
        payload = {"authorized": True, "job_id": "orphan-command", "timeout_s": 10,
                   "script": script, "sha256": hashlib.sha256(script.encode()).hexdigest()}
        queued, error = self.server.expanded_store.queue_admin(ident, "admin.exec", payload)
        self.assertIsNone(error)
        real_post = broker.post_json
        result_attempts = []

        def execute(value):
            subprocess.run(["/bin/sh", "-c", value["script"]], check=True)
            return {"ok": True, "exit_code": "0"}

        def post(url, envelope, **kwargs):
            if url.endswith("/node/result"):
                result_attempts.append(1)
                self.assertEqual(marker.read_text(), "x")
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(2)
                self.server = broker.make_server(port=self.port, state_dir=self.central)
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
                self.assertIsNone(self.server.expanded_store.get_result(queued["request_id"]))
            return real_post(url, envelope, **kwargs)

        with mock.patch.object(broker, "post_json", side_effect=post), \
                mock.patch("framebridge_expanded.runtime.adapter_request", side_effect=execute) as adapter, \
                mock.patch.object(node, "time", wraps=time) as clock:
            with self.assertRaisesRegex(broker.BrokerError, "^BROKER_HTTP_REJECTED_400$"):
                node.run(state)
            adapter.assert_called_once()
            clock.sleep.assert_not_called()
        self.assertEqual(result_attempts, [1])
        self.assertEqual(marker.read_text(), "x")
        # A later operator restart may poll, but must never reconstruct/replay
        # the orphaned command from its lost acknowledgement.
        self.assertTrue(node.run(state, once=True)["idle"])
        self.assertEqual(marker.read_text(), "x")

    def test_gateway_failures_recover_without_replaying_signed_envelope(self):
        state, _ = self.pair("gateway-node")
        original = self.server.RequestHandlerClass.do_POST
        codes, waits, successes = [502, 503, 504], [], []

        def gateway(handler):
            if codes:
                handler.send_json(codes.pop(0), {"ok": False})
                return
            successes.append(True)
            return original(handler)

        def pause(seconds):
            if successes:
                raise StopLoop
            waits.append(seconds)

        with mock.patch.object(self.server.RequestHandlerClass, "do_POST", gateway), \
                mock.patch.object(node, "time", wraps=time) as clock, \
                self.assertLogs(node._LOG, level="WARNING"):
            clock.sleep.side_effect = pause
            with self.assertRaises(StopLoop):
                node.run(state)
        self.assertEqual(waits, [1, 2, 4])

    def test_auth_and_invalid_json_fail_without_retry(self):
        state, _ = self.pair("reject-node")
        for code, body in [(403, b'{"ok":false}'), (500, b'{"ok":false}'),
                           (200, b'not-json'), (200, b'{"ok":false}')]:
            with self.subTest(code=code, body=body):
                attempts = []

                def reject(handler):
                    attempts.append(1)
                    handler.send_response(code)
                    handler.send_header("Content-Length", str(len(body)))
                    handler.end_headers()
                    handler.wfile.write(body)

                with mock.patch.object(self.server.RequestHandlerClass, "do_POST", reject), \
                        mock.patch.object(node, "time", wraps=time) as clock:
                    with self.assertRaises((broker.BrokerError, node.NodeError)):
                        node.run(state)
                    clock.sleep.assert_not_called()
                self.assertEqual(len(attempts), 1)

    def test_tls_and_unknown_transport_causes_are_terminal(self):
        state, _ = self.pair("tls-node")
        for cause in [urllib.error.URLError(ssl.SSLCertVerificationError("untrusted test CA")),
                      urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "invalid host")),
                      OSError(errno.EACCES, "denied"), ValueError("invalid response")]:
            with self.subTest(cause=type(cause).__name__), \
                    mock.patch("urllib.request.OpenerDirector.open", side_effect=cause) as opened, \
                    mock.patch.object(node, "time", wraps=time) as clock:
                with self.assertRaises(broker.BrokerError):
                    node.run(state)
                self.assertEqual(opened.call_count, 1)
                clock.sleep.assert_not_called()

    def test_once_is_fail_fast_and_retry_wait_propagates_cancellation(self):
        state, _ = self.pair("cancel-node")
        attempts = []

        def fault(handler):
            attempts.append(1)
            self.disconnect(handler)

        with mock.patch.object(self.server.RequestHandlerClass, "do_POST", fault), \
                mock.patch.object(node, "time", wraps=time) as clock:
            with self.assertRaises(broker.BrokerError):
                node.run(state, once=True)
            clock.sleep.assert_not_called()
            clock.sleep.side_effect = KeyboardInterrupt
            with self.assertLogs(node._LOG, level="WARNING"), self.assertRaises(KeyboardInterrupt):
                node.run(state)
            self.assertEqual(len(attempts), 2)
            clock.sleep.assert_called_once_with(1)

    def test_real_broker_restart_recovers_same_node_process_and_identity(self):
        state, ident = self.pair("restart-node")
        identity_before = (state / "node/identity.json").read_bytes()
        code = "from framebridge_expanded.node import run; run(state_dir=" + repr(str(state)) + ")"
        child = subprocess.Popen([sys.executable, "-u", "-c", code],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def wait_ready():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                self.assertIsNone(child.poll(), "node exited during recovery")
                if self.server.expanded_store.node_runtime_status(ident)["ready"]:
                    return
                threading.Event().wait(0.02)
            self.fail("node heartbeat did not recover")

        try:
            wait_ready()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
            with selectors.DefaultSelector() as selector:
                selector.register(child.stderr, selectors.EVENT_READ)
                self.assertTrue(selector.select(10), "child did not report broker interruption")
                self.assertIn("NODE_BROKER_RETRY", child.stderr.readline())
            self.assertIsNone(child.poll())
            self.server = broker.make_server(port=self.port, state_dir=self.central)
            self.assertFalse(self.server.expanded_store.node_runtime_status(ident)["ready"])
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            wait_ready()
            self.assertEqual((state / "node/identity.json").read_bytes(), identity_before)
            self.assertEqual(broker.list_confirmed_nodes()[0]["node_id"], ident)
        finally:
            child.terminate()
            try:
                child.wait(timeout=3)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                child.stderr.close()

    def test_sigterm_stops_real_process_during_retry_wait(self):
        state, _ = self.pair("stop-node")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        code = "from framebridge_expanded.node import run; run(state_dir=" + repr(str(state)) + ")"
        child = subprocess.Popen([sys.executable, "-u", "-c", code],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stderr, selectors.EVENT_READ)
                self.assertTrue(selector.select(10), "child did not report retry")
                self.assertIn("NODE_BROKER_RETRY", child.stderr.readline())
            started = time.monotonic()
            child.terminate()
            self.assertEqual(child.wait(timeout=3), -signal.SIGTERM)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stderr.close()


if __name__ == "__main__":
    unittest.main()
