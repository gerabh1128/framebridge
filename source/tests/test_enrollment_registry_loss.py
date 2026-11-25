"""Reproduce lost central registration without using any production identity."""
from contextlib import closing
import hashlib
import json
import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path

from framebridge_expanded import broker, node
import test_fleet_broker_node as fixtures


class RegistryLossTests(unittest.TestCase):
    setUp = fixtures.BrokerNodeTests.setUp
    tearDown = fixtures.BrokerNodeTests.tearDown
    make_node = fixtures.BrokerNodeTests.make_node
    pair = fixtures.BrokerNodeTests.pair

    def switch_to_empty_test_registry(self):
        # Two distinct, disposable central profiles; no registry is edited or
        # transferred. Bind the same loopback port to simulate recovery from
        # an older backup of the same central.
        original = self.central
        replacement = Path(tempfile.mkdtemp(prefix="replacement-central-", dir=self.root))
        (replacement / "config.json").write_bytes(
            (original / "config.json").read_bytes())
        before = (original / "broker/paired-nodes.json").read_bytes()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.central = replacement
        self.server = broker.make_server(port=self.port, state_dir=replacement)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        broker.configure(state_dir=replacement)
        return original, before

    def test_missing_registry_rejects_original_without_changing_identity_or_history(self):
        profile, ident = self.pair("synthetic-lost-node")
        original_status = node.status(profile)
        identity = (profile / "node/identity.json").read_bytes()
        pairing = (profile / "node/pairing.json").read_bytes()
        # Synthetic local history is deliberately separate from enrollment.
        history = profile / "synthetic-history.sqlite3"
        with closing(sqlite3.connect(history)) as c:
            c.execute("CREATE TABLE completed_jobs(id TEXT PRIMARY KEY)")
            c.execute("INSERT INTO completed_jobs VALUES ('test-job')")
            c.commit()
        history_digest = hashlib.sha256(history.read_bytes()).hexdigest()

        old_central, before = self.switch_to_empty_test_registry()
        self.assertEqual(broker.list_confirmed_nodes(), [])
        with self.assertRaisesRegex(broker.BrokerError, "BROKER_HTTP_REJECTED_403"):
            node.run(profile, once=True)
        self.assertEqual(node.status(profile)["node_id"], ident)
        self.assertEqual(node.status(profile)["fingerprint"],
                         original_status["fingerprint"])
        with self.assertRaisesRegex(node.NodeError, "NODE_ENROLLMENT_ALREADY_EXISTS"):
            node.enroll(self.url, "synthetic-lost-node", profile)
        self.assertEqual((profile / "node/identity.json").read_bytes(), identity)
        self.assertEqual((profile / "node/pairing.json").read_bytes(), pairing)
        self.assertEqual(hashlib.sha256(history.read_bytes()).hexdigest(), history_digest)
        self.assertEqual((old_central / "broker/paired-nodes.json").read_bytes(), before)
        self.assertEqual(broker.list_pending(), [])
        self.assertEqual(broker.list_confirmed_nodes(), [])

    def test_fresh_request_still_requires_explicit_matching_approval(self):
        old, _ = self.pair("synthetic-old-node")
        old_identity = (old / "node/identity.json").read_bytes()
        old_central, before = self.switch_to_empty_test_registry()
        candidate = self.make_node("synthetic-new-node")
        pending = node.enroll(self.url, "synthetic-new-node", candidate)
        self.assertEqual(broker.list_confirmed_nodes(), [])
        with self.assertRaises(node.NodeError):
            node.run(candidate, once=True)
        item = broker.list_pending()[0]
        self.assertEqual(item["node_id"], pending["node_id"])
        self.assertEqual(item["fingerprint"], pending["fingerprint"])
        with self.assertRaises(broker.BrokerError):
            broker.approve(item["request_id"], node.status(old)["node_id"])
        self.assertEqual(broker.list_confirmed_nodes(), [])
        approved = broker.approve(item["request_id"], pending["node_id"])
        self.assertTrue(approved["ok"])
        self.assertEqual(node.poll_enrollment(candidate)["state"], "paired")
        self.assertTrue(node.run(candidate, once=True)["idle"])
        self.assertEqual(broker.list_confirmed_nodes()[0]["node_id"], pending["node_id"])
        with self.assertRaisesRegex(broker.BrokerError, "BROKER_HTTP_REJECTED_403"):
            node.run(old, once=True)
        self.assertEqual((old / "node/identity.json").read_bytes(), old_identity)
        self.assertEqual((old_central / "broker/paired-nodes.json").read_bytes(), before)
