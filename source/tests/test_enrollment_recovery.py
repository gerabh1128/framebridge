"""Real loopback enrollment protocol; only synthetic profiles and unit mocks."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest
import threading
from unittest import mock

from framebridge_expanded import autostart, broker, cli, config, node, enrollment_recovery
import test_enrollment_registry_loss as fixture


class EnrollmentRecoveryTests(unittest.TestCase):
    setUp = fixture.RegistryLossTests.setUp
    tearDown = fixture.RegistryLossTests.tearDown
    pair = fixture.RegistryLossTests.pair

    def make_node(self, label):
        state = fixture.RegistryLossTests.make_node(self, label)
        if getattr(self, "test_ca", None):
            p = state / "config.json"
            settings = json.loads(p.read_text())
            settings["tls_ca"] = str(self.test_ca)
            p.write_text(json.dumps(settings))
        return state

    def enable_tls(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        cert = self.root / "synthetic-ca.pem"
        key = self.root / "synthetic-server-key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-nodes", "-days", "1", "-keyout", str(key),
                        "-out", str(cert), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.test_ca = cert
        self.url = "https://127.0.0.1:" + str(self.port)
        p = self.central / "config.json"
        settings = json.loads(p.read_text())
        settings.update(broker_url=self.url, tls_ca=str(cert),
                        tls_cert=str(cert), tls_key=str(key))
        p.write_text(json.dumps(settings))
        self.server = broker.make_server(port=self.port, state_dir=self.central)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        broker.configure(state_dir=self.central)
    switch_to_empty_test_registry = fixture.RegistryLossTests.switch_to_empty_test_registry

    def prepare(self):
        profile, ident = self.pair("synthetic-recovery-node")
        settings = config.default_config(role="node", name="synthetic-recovery-node")
        settings.update(node_id=ident, broker_url=self.url)
        if getattr(self, "test_ca", None):
            settings["tls_ca"] = str(self.test_ca)
        config.save_config(profile, settings)
        private_home = self.root / "synthetic-owner-home"
        private_home.mkdir(mode=0o700)
        unit_dir = private_home / ".config/systemd/user"
        unit_dir.mkdir(parents=True, mode=0o700)
        home = mock.patch.object(Path, "home", return_value=private_home)
        home.start()
        self.addCleanup(home.stop)
        _, directory, unit = autostart._paths(profile)
        unit.write_text("# synthetic managed node unit\n")
        unit.chmod(0o600)
        autostart._json(directory / "installation.json",
                        {"schema": 1, "node": True, "unit": unit.name,
                         "unit_sha256": hashlib.sha256(unit.read_bytes()).hexdigest()})
        self.assertTrue(autostart.mark_manual_stop(profile))
        system = mock.patch.object(
            autostart, "_systemctl",
            return_value=subprocess.CompletedProcess(
                [], 0, b"ActiveState=inactive\nMainPID=0\n", b""))
        self.system = system.start()
        self.addCleanup(system.stop)
        fbroot = profile / "home/.local/share/framebridge/fbterm"
        fbroot.mkdir(parents=True, mode=0o700)
        queue = fbroot / "queue-v2"
        queue.mkdir(mode=0o700)
        (queue / "scheduler.lock").touch(mode=0o600)
        db = queue / "jobs.sqlite3"
        with closing(sqlite3.connect(db)) as c:
            c.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT NOT NULL)")
            c.execute("INSERT INTO jobs VALUES ('synthetic-completed','completed')")
            c.commit()
        db.chmod(0o600)
        self.profile = profile
        self.db = db
        self.ident = ident
        self.fingerprint = node.status(profile)["fingerprint"]
        self.before_identity = (profile / "node/identity.json").read_bytes()
        self.before_config = (profile / "config.json").read_bytes()
        self.before_history = hashlib.sha256(db.read_bytes()).hexdigest()
        self.before_pairing = (profile / "node/pairing.json").read_bytes()
        self.journal = profile / "node/enrollment-recovery.json"
        self.old_central, self.old_registry = self.switch_to_empty_test_registry()

    def recover(self, **overrides):
        args = dict(expected_node_id=self.ident,
                    expected_fingerprint=self.fingerprint,
                    expected_broker=self.url,
                    confirm=lambda summary: True)
        args.update(overrides)
        return enrollment_recovery.recover_enrollment(self.profile, **args)

    def assert_preserved(self):
        self.assertEqual((self.profile / "node/identity.json").read_bytes(),
                         self.before_identity)
        self.assertEqual((self.profile / "config.json").read_bytes(),
                         self.before_config)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),
                         self.before_history)
        self.assertEqual((self.old_central / "broker/paired-nodes.json").read_bytes(),
                         self.old_registry)

    def assert_unchanged(self):
        self.assert_preserved()
        self.assertEqual((self.profile / "node/pairing.json").read_bytes(),
                         self.before_pairing)
        self.assertFalse(self.journal.exists())
        self.assertEqual(broker.list_pending(), [])

    def test_recovery_requires_new_approval_and_preserves_identity_history(self):
        self.prepare()
        with self.assertRaisesRegex(broker.BrokerError, "BROKER_HTTP_REJECTED_403"):
            node.run(self.profile, once=True)
        result = self.recover()
        self.assertEqual(result["state"], "approval_required")
        self.assertFalse(result["runtime_started"])
        self.assertEqual(result["historical_jobs"], 1)
        self.assert_preserved()
        self.assertEqual(broker.list_confirmed_nodes(), [])
        with self.assertRaisesRegex(node.NodeError, "NODE_NOT_CONFIRMED"):
            node.run(self.profile, once=True)
        item = broker.list_pending()[0]
        self.assertEqual(item["node_id"], self.ident)
        self.assertEqual(item["fingerprint"], self.fingerprint)
        with self.assertRaises(broker.BrokerError):
            broker.approve(item["request_id"], "00000000-0000-0000-0000-000000000000")
        self.assertEqual(broker.list_confirmed_nodes(), [])
        self.assertTrue(broker.approve(item["request_id"], self.ident)["ok"])
        self.assertEqual(node.poll_enrollment(self.profile)["state"], "paired")
        self.assertTrue(node.run(self.profile, once=True)["idle"])
        self.assertEqual(broker.list_confirmed_nodes()[0]["node_id"], self.ident)
        self.assertTrue(broker.list_confirmed_nodes()[0]["ready"])
        self.assert_preserved()
        self.assertEqual(json.loads(self.journal.read_text())["state"],
                         "confirmed")

    def test_wrong_identity_broker_and_cancel_leave_old_association_unchanged(self):
        self.prepare()
        for overrides, expected in (
                ({"expected_fingerprint": "0"*64}, "RECOVERY_IDENTITY_MISMATCH"),
                ({"expected_node_id": "00000000-0000-0000-0000-000000000000"},
                 "RECOVERY_IDENTITY_MISMATCH"),
                ({"expected_broker": "https://example.invalid:20878"},
                 "RECOVERY_BROKER_MISMATCH"),
                ({"confirm": lambda summary: False}, "RECOVERY_CONFIRMATION_REQUIRED")):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(node.NodeError, expected):
                    self.recover(**overrides)
                self.assert_unchanged()

    def test_active_unit_and_agent_lock_prevent_recovery(self):
        self.prepare()
        self.system.return_value = subprocess.CompletedProcess(
            [], 0, b"ActiveState=active\nMainPID=99\n", b"")
        with self.assertRaisesRegex(node.NodeError, "RECOVERY_STOP_MANAGED_NODE_FIRST"):
            self.recover()
        self.assert_unchanged()
        self.system.return_value = subprocess.CompletedProcess(
            [], 0, b"ActiveState=inactive\nMainPID=0\n", b"")
        with node._operation_lock(self.profile):
            with self.assertRaisesRegex(RuntimeError, "RECOVERY_PROFILE_OR_ENGINE_ACTIVE"):
                self.recover()
        self.assert_unchanged()

    def test_pending_or_uncertain_work_prevents_recovery(self):
        self.prepare()
        for state in ("queued", "in_flight", "outcome_unknown"):
            with self.subTest(state=state):
                with closing(sqlite3.connect(self.db)) as c:
                    c.execute("UPDATE jobs SET state=?", (state,))
                    c.commit()
                with self.assertRaisesRegex(node.NodeError, "RECOVERY_WORK_NOT_QUIESCENT"):
                    self.recover()
                self.assertEqual((self.profile / "node/pairing.json").read_bytes(),
                                 self.before_pairing)
                self.assertFalse(self.journal.exists())
                self.assertEqual(broker.list_pending(), [])

    def test_uncertain_submission_is_not_replayed(self):
        self.prepare()
        with mock.patch.object(node, "_submit_enrollment",
                               side_effect=broker.BrokerError("synthetic transport interruption")):
            with self.assertRaisesRegex(node.NodeError, "RECOVERY_SUBMISSION_REVIEW_REQUIRED"):
                self.recover()
        self.assertEqual(node.status(self.profile)["state"], "paired")
        self.assertEqual(node.status(self.profile)["agent_state"], "blocked_for_recovery")
        self.assertEqual((self.profile / "node/pairing.json").read_bytes(),
                         self.before_pairing)
        with self.assertRaisesRegex(node.NodeError, "RECOVERY_SUBMISSION_REVIEW_REQUIRED"):
            node.run(self.profile, once=True)
        with self.assertRaisesRegex(node.NodeError, "RECOVERY_SUBMISSION_REVIEW_REQUIRED"):
            node.poll_enrollment(self.profile)
        self.assertEqual(json.loads(self.journal.read_text())["state"],
                         "submission_review_required")
        with self.assertRaises(node.NodeError):
            self.recover()
        with self.assertRaisesRegex(node.NodeError, "RECOVERY_PREVIOUS_ATTEMPT_REVIEW_REQUIRED"):
            node.enroll(self.url, "synthetic-recovery-node", self.profile)
        self.assertEqual(broker.list_confirmed_nodes(), [])
        self.assert_preserved()

    def test_interruption_after_pending_commit_resumes_only_existing_request(self):
        self.prepare()
        original_write = node._write_json

        def interrupt(path, value):
            if path == self.journal and value.get("state") == "approval_required":
                raise OSError("synthetic interrupted journal update")
            return original_write(path, value)

        with mock.patch.object(node, "_write_json", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(node.status(self.profile)["state"], "pair_requested")
        self.assertEqual(json.loads(self.journal.read_text())["state"],
                         "submission_review_required")
        pending = broker.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["node_id"], self.ident)
        self.assertEqual(pending[0]["fingerprint"], self.fingerprint)
        self.assertEqual(node.poll_enrollment(self.profile)["state"], "pair_requested")
        self.assertEqual(len(broker.list_pending()), 1)
        self.assertTrue(broker.approve(pending[0]["request_id"], self.ident)["ok"])
        deadline = time.monotonic() + 15
        while True:
            result = node.poll_enrollment(self.profile)
            if result["state"] == "paired":
                break
            self.assertEqual(result.get("broker_status"), "local_poll_interval")
            delay = result["retry_after"]
            self.assertLess(time.monotonic() + delay, deadline)
            time.sleep(delay)
        self.assertEqual(json.loads(self.journal.read_text())["state"], "confirmed")
        self.assertTrue(node.run(self.profile, once=True)["idle"])
        self.assert_preserved()

    def approve_existing_request(self):
        pending = [x for x in broker.list_pending() if x["node_id"] == self.ident]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["fingerprint"], self.fingerprint)
        self.assertTrue(broker.approve(pending[0]["request_id"], self.ident)["ok"])
        self.assertEqual(node.poll_enrollment(self.profile)["state"], "paired")
        self.assertTrue(node.run(self.profile, once=True)["idle"])

    def test_verified_tls_recovery_and_second_recovery_keep_history(self):
        self.enable_tls()
        self.prepare()
        self.assertEqual(self.recover()["state"], "approval_required")
        self.approve_existing_request()
        first = self.journal.read_bytes()
        self.assertEqual(json.loads(first)["state"], "confirmed")
        old_central, old_registry = self.switch_to_empty_test_registry()
        self.assertEqual(self.recover()["state"], "approval_required")
        self.approve_existing_request()
        archive = self.profile / "node/enrollment-recovery-history"
        self.assertEqual(len(list(archive.iterdir())), 1)
        self.assertEqual(next(archive.iterdir()).read_bytes(), first)
        self.assertEqual((old_central / "broker/paired-nodes.json").read_bytes(),
                         old_registry)
        self.assert_preserved()

    def test_untrusted_tls_cannot_replace_previous_association(self):
        self.enable_tls()
        self.prepare()
        other_cert = self.root / "synthetic-other-ca.pem"
        other_key = self.root / "synthetic-other-key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-nodes", "-days", "1", "-keyout", str(other_key),
                        "-out", str(other_cert), "-subj", "/CN=other-ca"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        settings = config.load_config(self.profile)
        settings["tls_ca"] = str(other_cert)
        config.save_config(self.profile, settings)
        self.before_config = (self.profile / "config.json").read_bytes()
        with self.assertRaisesRegex(node.NodeError, "RECOVERY_SUBMISSION_REVIEW_REQUIRED"):
            self.recover()
        self.assertEqual((self.profile / "node/pairing.json").read_bytes(),
                         self.before_pairing)
        self.assertEqual(node.status(self.profile)["agent_state"], "blocked_for_recovery")
        self.assertEqual(broker.list_pending(), [])
        self.assertEqual(broker.list_confirmed_nodes(), [])
        self.assert_preserved()

    def test_existing_alias_conflict_cannot_gain_a_second_approval(self):
        self.prepare()
        conflict = self.make_node("synthetic-conflicting-profile")
        result = node.enroll(self.url, "synthetic-recovery-node", conflict)
        pending = broker.list_pending()[0]
        self.assertTrue(broker.approve(pending["request_id"], result["node_id"])["ok"])
        self.assertEqual(node.poll_enrollment(conflict)["state"], "paired")
        self.assertTrue(node.run(conflict, once=True)["idle"])
        self.assertEqual(self.recover()["state"], "approval_required")
        item = next(x for x in broker.list_pending() if x["node_id"] == self.ident)
        with self.assertRaises(broker.BrokerError):
            broker.approve(item["request_id"], self.ident)
        self.assertEqual(len(broker.list_confirmed_nodes()), 1)
        self.assertEqual(broker.list_confirmed_nodes()[0]["node_id"], result["node_id"])
        with self.assertRaises(node.NodeError):
            node.run(self.profile, once=True)
        self.assert_preserved()

    def test_recovery_cli_never_accepts_noninteractive_execution(self):
        self.prepare()
        args = cli.build_parser().parse_args([
            "--state-dir", str(self.profile), "--non-interactive",
            "nodes", "recover-enrollment", "--node-id", self.ident,
            "--fingerprint", self.fingerprint, "--central", self.url])
        with mock.patch.object(cli, "_interaction_required", return_value=2) as required:
            self.assertEqual(cli.nodes_command(args, self.profile), 2)
        required.assert_called_once_with("fb nodes recover-enrollment")
        self.assert_unchanged()

    def test_environment_cannot_replace_saved_tls_trust(self):
        self.prepare()
        with mock.patch.dict(os.environ, {"FRAMEBRIDGE_TLS_CA": "/synthetic-unreviewed-ca.pem"}):
            with self.assertRaisesRegex(node.NodeError, "RECOVERY_TLS_TRUST_OVERRIDE"):
                self.recover()
        self.assert_unchanged()


    def test_legacy_readable_node_directory_is_restricted_only_after_confirmation(self):
        self.prepare()
        directory = self.profile / "node"
        directory.chmod(0o755)
        with self.assertRaisesRegex(node.NodeError, "RECOVERY_CONFIRMATION_REQUIRED"):
            self.recover(confirm=lambda summary: False)
        self.assertEqual(directory.stat().st_mode & 0o7777, 0o755)
        self.assert_unchanged()
        seen = []
        result = self.recover(confirm=lambda summary: (seen.append(summary), True)[1])
        self.assertEqual(seen[0]["node_directory_mode_before"], "0755")
        self.assertEqual(seen[0]["node_directory_mode_after"], "0700")
        self.assertEqual(directory.stat().st_mode & 0o7777, 0o700)
        self.assertEqual(result["state"], "approval_required")
        self.assert_preserved()

    def test_legacy_group_writable_directory_is_rejected(self):
        self.prepare()
        directory = self.profile / "node"
        directory.chmod(0o775)
        with self.assertRaisesRegex(RuntimeError, "RECOVERY_UNTRUSTED_DIRECTORY"):
            self.recover()
        self.assertEqual(directory.stat().st_mode & 0o7777, 0o775)
        self.assert_unchanged()

    def test_legacy_directory_mode_changed_during_confirmation_is_rejected(self):
        self.prepare()
        directory = self.profile / "node"
        directory.chmod(0o755)
        def confirm(summary):
            directory.chmod(0o750)
            return True
        with self.assertRaisesRegex(node.NodeError, "RECOVERY_NODE_DIRECTORY_CHANGED"):
            self.recover(confirm=confirm)
        self.assertEqual(directory.stat().st_mode & 0o7777, 0o750)
        self.assert_unchanged()
