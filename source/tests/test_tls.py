"""Real TLS and certificate-bootstrap checks in isolated application profiles."""
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import stat
import tempfile
import threading
import unittest
from unittest import mock

from framebridge_expanded import broker, node, tls
from framebridge_expanded.config import default_config, load_config, save_config

ROOT = Path(__file__).resolve().parents[1]


class CertificateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tls-test-", dir=ROOT / "qa")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def profile(self, name, role="central"):
        state = self.root / name
        save_config(state, default_config(role, name))
        return state

    def test_certificate_sans_fingerprint_private_modes_and_config(self):
        state = self.profile("cert-central")
        result = tls.create_certificates(state, "central.example.test", "192.0.2.10")
        cfg = load_config(state)
        x509, hashes, serialization, *_ = tls._crypto()
        ca = x509.load_pem_x509_certificate(Path(result["ca_public_file"]).read_bytes())
        cert = x509.load_pem_x509_certificate(Path(result["server_certificate_file"]).read_bytes())
        self.assertEqual(result["ca_fingerprint_sha256"], ca.fingerprint(hashes.SHA256()).hex())
        self.assertEqual(result["server_fingerprint_sha256"], cert.fingerprint(hashes.SHA256()).hex())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(set(san.get_values_for_type(x509.DNSName)), {"central.example.test", "localhost"})
        self.assertEqual(set(san.get_values_for_type(x509.IPAddress)), {
            ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")})
        self.assertTrue(ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertEqual(stat.S_IMODE((state / "tls").stat().st_mode), 0o700)
        for path in (state / "tls").iterdir():
            expected = 0o644 if path.name == "ca-public.pem" else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, path.name)
        self.assertEqual(cfg["tls_ca"], result["ca_public_file"])
        self.assertEqual(cfg["broker_url"], "https://192.0.2.10:20878")
        self.assertEqual(cfg["broker_control_url"], "https://127.0.0.1:20878")
        self.assertEqual(cfg["broker_listen_host"], "127.0.0.1")
        self.assertFalse(result["private_key_values_exported"])
        self.assertFalse(result["openai_credentials_created_or_saved"])
        self.assertFalse(any("private.key" in key for key in result))

    def test_existing_material_is_never_overwritten(self):
        state = self.profile("no-overwrite")
        tls.create_certificates(state, "central.test", "127.0.0.1")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (state / "tls").iterdir()}
        with self.assertRaises(FileExistsError):
            tls.create_certificates(state, "replacement.test", "127.0.0.1")
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (state / "tls").iterdir()}
        self.assertEqual(before, after)

    def test_invalid_or_active_profile_never_creates_material(self):
        standalone = self.profile("standalone", "standalone")
        with self.assertRaises(ValueError):
            tls.create_certificates(standalone, "central.test", "127.0.0.1")
        state = self.profile("active")
        with mock.patch("framebridge_expanded.runtime.status", return_value={"running": True}):
            with self.assertRaises(RuntimeError):
                tls.create_certificates(state, "central.test", "127.0.0.1")
        with self.assertRaises(ValueError):
            tls.create_certificates(state, "bad host", "127.0.0.1")
        with self.assertRaises(ValueError):
            tls.create_certificates(state, "central.test", "0.0.0.0")
        self.assertFalse((state / "tls").exists())

    def test_control_uses_concrete_listener_and_migrates_old_default_without_writing_config(self):
        cases = (("127.0.0.2", "127.0.0.2", socket.AF_INET),
                 ("::", "[::1]", socket.AF_INET6))
        for index, (listen, authority, family) in enumerate(cases):
            with self.subTest(listen=listen):
                try:
                    with socket.socket(family) as probe:
                        probe.bind((listen, 0))
                        port = probe.getsockname()[1]
                except OSError:
                    if family == socket.AF_INET6:
                        self.skipTest("IPv6 loopback is unavailable.")
                    raise
                state = self.profile("specific-broker-" + str(index))
                cfg = load_config(state)
                cfg["ports"]["broker"] = port
                save_config(state, cfg)
                public = tls.create_certificates(state, "central.test", "192.0.2.10", listen_host=listen)
                expected = f"https://{authority}:{port}"
                self.assertEqual(public["broker_control_url"], expected)
                server = broker.make_server(listen, port, state)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    broker.configure(state_dir=state)
                    self.assertEqual(broker.list_confirmed_nodes(), [])
                    cfg = load_config(state)
                    cfg["broker_control_url"] = f"https://127.0.0.1:{port}"
                    save_config(state, cfg)
                    broker.configure(state_dir=state)
                    self.assertEqual(broker.list_confirmed_nodes(), [])
                    self.assertEqual(load_config(state)["broker_control_url"], cfg["broker_control_url"])
                    broker.configure(broker_url=expected + "/explicit", state_dir=state)
                    self.assertEqual(broker._settings()[1], expected + "/explicit")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

    def test_only_control_to_configured_concrete_https_listener_binds_loopback(self):
        cases = (("192.0.2.10", "192.0.2.10", "127.0.0.1"),
                 ("2001:db8::10", "[2001:db8::10]", "::1"))
        for listen, authority, source in cases:
            cfg = {"broker_listen_host": listen, "ports": {"broker": 20878}}
            base = f"https://{authority}:20878"
            with self.subTest(listen=listen), mock.patch.object(broker, "_config", return_value=cfg), \
                 mock.patch.object(broker, "_settings", return_value=(None, None, None)), \
                 mock.patch.object(broker, "_private_read", return_value=b"disposable-test-secret"), \
                 mock.patch.object(broker.urllib.request, "build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
                self.assertTrue(broker.post_json(base + "/fbpair/v1/control/nodes", {}, control=True)["ok"])
                handler = build.call_args.args[-1]
                self.assertIsInstance(handler, broker._ControlHTTPSHandler)
                with mock.patch.object(handler, "do_open") as open_https:
                    handler.https_open(object())
                connector = open_https.call_args.args[0]
                self.assertEqual(connector.keywords["source_address"], (source, 0))
                broker.post_json(base + "/fbpair/v1/node/poll", {})
                self.assertNotIsInstance(build.call_args.args[-1], broker._ControlHTTPSHandler)
                broker.post_json("https://localhost:20878/fbpair/v1/control/nodes", {}, control=True)
                self.assertNotIsInstance(build.call_args.args[-1], broker._ControlHTTPSHandler)

    def test_real_https_node_enrollment_and_wrong_ca_rejection(self):
        # Override only with a local, assigned address to exercise an actual LAN route.
        address = os.environ.get("FRAMEBRIDGE_TLS_TEST_ADDRESS", "127.0.0.1")
        ipaddress.ip_address(address)
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", 0))
            port = probe.getsockname()[1]
        central = self.profile("https-central")
        cfg = load_config(central)
        cfg["ports"]["broker"] = port
        if port in (cfg["ports"]["mcp"], cfg["ports"]["hub"]):
            self.skipTest("Ephemeral broker port overlaps a default service port.")
        save_config(central, cfg)
        public = tls.create_certificates(central, "central.test", address, listen_host="0.0.0.0")
        server = broker.make_server("0.0.0.0", port, central)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            joining = self.profile("https-node", "node")
            nc = load_config(joining)
            nc.update(broker_url=public["broker_url"], tls_ca=public["ca_public_file"])
            save_config(joining, nc)
            enrolled = node.enroll(public["broker_url"], "https-node", joining)
            broker.configure(state_dir=central)
            pending = broker.list_pending()
            entry = next(p for p in pending if p["node_id"] == enrolled["node_id"])
            self.assertEqual(entry["fingerprint"], enrolled["fingerprint"])
            approved = broker.approve(entry["request_id"], enrolled["node_id"], central)
            polled = node.poll_enrollment(joining)
            self.assertEqual(polled["state"], "paired")
            self.assertFalse(polled.get("broker_confirm_required", False))
            self.assertTrue(any(p["node_id"] == enrolled["node_id"] for p in broker.list_confirmed_nodes()))

            wrong = self.profile("wrong-authority")
            wrong_public = tls.create_certificates(wrong, "other.test", address)
            rejected = self.profile("wrong-ca-node", "node")
            rc = load_config(rejected)
            rc.update(broker_url=public["broker_url"], tls_ca=wrong_public["ca_public_file"])
            save_config(rejected, rc)
            before = broker.list_pending()
            with self.assertRaises(Exception) as failure:
                node.enroll(public["broker_url"], "wrong-ca-node", rejected)
            # Check the verification error, rather than accepting any transport failure.
            chain = []
            error = failure.exception
            for _ in range(6):
                if error is None:
                    break
                chain.append(str(error))
                error = error.__cause__ or error.__context__
            self.assertIn("CERTIFICATE_VERIFY_FAILED", " ".join(chain))
            self.assertEqual(before, broker.list_pending())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
