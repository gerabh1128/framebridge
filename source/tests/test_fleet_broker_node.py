"""Real FBPAIR/1 identities and sockets in temporary, isolated profiles."""
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from framebridge_expanded import broker, node


class BrokerNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="framebridge-expanded-broker-test-")
        self.root = Path(self.temp.name)
        self.central = self.root / "central"
        self.central.mkdir(mode=0o700)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = "http://127.0.0.1:" + str(self.port)
        (self.central / "config.json").write_text(json.dumps({
            "schema": 1, "role": "central", "name": "central-test", "node_id": str(uuid.uuid4()),
            "ports": {"broker": self.port}, "broker_url": self.url}))
        self.server = broker.make_server(port=self.port, state_dir=self.central)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        broker.configure(state_dir=self.central)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def make_node(self, label):
        state = self.root / label
        state.mkdir(mode=0o700)
        (state / "config.json").write_text(json.dumps({
            "schema": 1, "role": "node", "name": label, "node_id": str(uuid.uuid4()),
            "broker_url": self.url}))
        return state

    def pair(self, label):
        state = self.make_node(label)
        public = node.enroll(self.url, label, state)
        pending = next(p for p in broker.list_pending() if p["node_id"] == public["node_id"])
        response = broker.approve(pending["request_id"], public["node_id"])
        self.assertTrue(response["ok"])
        paired = node.poll_enrollment(state)
        self.assertEqual(paired["state"], "paired")
        self.assertFalse(node.status(state)["broker_confirm_required"])
        return state, public["node_id"]

    def test_http_remote_and_unauthenticated_control_are_rejected(self):
        for value in ("http://10.0.0.53:1234", "http://example.com", "https://u:p@example.com", "https://example.com/#x"):
            with self.assertRaises(broker.BrokerError):
                broker.validate_endpoint(value)
        with self.assertRaises(broker.BrokerError):
            broker.post_json(self.url + "/fbpair/v1/control/nodes", {}, state_dir=self.central)

    def test_approval_requires_exact_uuid_and_operator_auth(self):
        state = self.make_node("node-a")
        pending = node.enroll(self.url, "node-a", state)
        item = broker.list_pending()[0]
        self.assertEqual(item["node_id"], pending["node_id"])
        self.assertTrue(item["fingerprint"])
        self.assertEqual(item["fingerprint"], pending["fingerprint"])
        with self.assertRaises(broker.BrokerError):
            broker.approve(item["request_id"], str(uuid.uuid4()))
        self.assertEqual(broker.list_confirmed_nodes(), [])

    def test_two_nodes_identity_confirmation_heartbeat_and_real_read(self):
        one, first = self.pair("node-a")
        two, second = self.pair("node-b")
        self.assertNotEqual(first, second)
        before = broker.list_confirmed_nodes()
        self.assertEqual(len(before), 2)
        self.assertTrue(all(n["ready"] is False for n in before))
        for state in (one, two):
            self.assertTrue(node.run(state, once=True)["idle"])
        self.assertTrue(all(n["ready"] is True for n in broker.list_confirmed_nodes()))
        self.assertEqual(node.status(one)["node_id"], first)
        stopped = threading.Event()
        errors = []

        def pump():
            while not stopped.is_set():
                try:
                    node.run(one, once=True)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    return
                stopped.wait(0.05)

        worker = threading.Thread(target=pump, daemon=True)
        worker.start()
        try:
            result = broker.request(first, "system.status", {}, timeout_s=5)
            self.assertTrue(result["ok"], result)
            self.assertIsInstance(result["result"], dict)
            self.assertEqual(result["node_id"], first)
        finally:
            stopped.set()
            worker.join(2)
        self.assertFalse(errors)

    def test_dispatch_rejects_wrong_node_and_bad_digest_before_adapter(self):
        state, ident = self.pair("node-a")
        called = []
        script = "printf harmless"
        request = {"request_id": "request-test", "node_id": str(uuid.uuid4()),
                   "expires_at": time.time() + 30, "operation": "admin.exec", "payload": {
                       "authorized": True, "job_id": "test-job", "timeout_s": 10,
                       "script": script, "sha256": hashlib.sha256(script.encode()).hexdigest()}}
        self.assertEqual(node.dispatch_request(request, called.append, state)["error"], "REQUEST_NODE_MISMATCH")
        request["node_id"] = ident
        request["payload"]["sha256"] = "0" * 64
        self.assertEqual(node.dispatch_request(request, called.append, state)["error"], "SCRIPT_SHA256_MISMATCH")
        self.assertEqual(called, [])

    def test_confirmed_identity_survives_broker_restart_but_readiness_does_not(self):
        state, ident = self.pair("node-a")
        node.run(state, once=True)
        self.assertTrue(broker.list_confirmed_nodes()[0]["ready"])
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.server = broker.make_server(port=self.port, state_dir=self.central)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        nodes = broker.list_confirmed_nodes()
        self.assertEqual(nodes[0]["node_id"], ident)
        self.assertFalse(nodes[0]["ready"])
        node.run(state, once=True)
        self.assertTrue(broker.list_confirmed_nodes()[0]["ready"])


if __name__ == "__main__":
    unittest.main()
