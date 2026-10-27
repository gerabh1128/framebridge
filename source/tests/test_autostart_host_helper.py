"""Synthetic backend/lifecycle tests. No host credentials or sudo are used."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from framebridge_expanded import autostart, config, connection_background

ID = "tunnel_0123456789abcdef0123456789abcdef"
KEY = "SYNTHETIC-FRAMEBRIDGE-HOST-BACKEND-NOT-A-REAL-KEY"


class HostCredentialAutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"; self.home.mkdir(mode=0o700)
        self.state = self.home / "profile"
        value = config.default_config(); value.update(tunnel_id=ID, headless="no")
        config.save_config(self.state, value)
        self.settings = {"account_label": "owner@example.test", "tunnel_id": ID,
                         "control_plane_organization_id": "", "control_plane_url_path": ""}
        self.calls = []; self.system_calls = []; self.generation = 0
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Path, "home", return_value=self.home).start()
        self.command = mock.patch.object(autostart, "_command", side_effect=self.fake_command).start()
        self.systemctl = mock.patch.object(autostart, "_systemctl", side_effect=self.fake_systemctl).start()
        self.helper = mock.patch.object(autostart, "_helper_ready").start()
        mock.patch.object(autostart, "_linger", return_value=False).start()
        mock.patch.object(autostart, "_enabled_units", return_value=[]).start()
        mock.patch.object(autostart.os, "geteuid", return_value=1000).start()
        mock.patch.object(autostart.resource, "setrlimit").start()
        mock.patch("builtins.input", return_value="y").start()
        self.prompt = mock.patch.object(autostart.getpass, "getpass", return_value=KEY).start()
        mock.patch.object(autostart, "status", return_value={"configured": True}).start()
        mock.patch.object(connection_background, "status", return_value={"alive": False}).start()
        mock.patch.object(connection_background, "disconnect", return_value={"ok": True, "alive": False}).start()
        mock.patch("sys.stdout", new_callable=io.StringIO).start()

    def fake_command(self, args, **kwargs):
        self.calls.append((args, kwargs))
        output = b""
        if args[:2] == ["systemd-creds", "--version"]: output = b"systemd 255\n"
        elif "-ll" in args:
            command = " ".join(args[args.index("--") + 1:])
            output = ("Sudoers entry: /etc/sudoers.d/synthetic-test\n RunAsUsers: root\n Options: !authenticate, !log_input, !log_output, !setenv\n Commands:\n  " + command + "\n Matched: " + command + "\n").encode()
        elif "protect" in args or "encrypt" in args:
            self.generation += 1
            output = ("SYNTHETIC-HOST-CIPHER-%03d-" % self.generation).encode() * 8
        elif "decrypt" in args: output = KEY.encode()
        return subprocess.CompletedProcess(args, 0, output, b"")

    def fake_systemctl(self, *args, **kwargs):
        self.system_calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def operations(self, name):
        return [(args, options) for args, options in self.calls if name in args and "-ll" not in args]

    def test_255_setup_uses_helper_stdin_and_preserves_user_unit(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        _, directory, unit = autostart._paths(self.state)
        record = autostart._manifest(directory)
        self.assertEqual(autostart.HOST_BACKEND, record["credential_backend"])
        args, options = self.operations("protect")[0]
        self.assertEqual(["sudo", "-n", "--", str(autostart.HELPER), "protect", autostart._profile_id(self.state)], args)
        self.assertEqual(KEY.encode(), options["data"])
        self.assertFalse(self.operations("encrypt"))
        self.assertNotIn(KEY, " ".join(args))
        self.assertNotIn("User=", unit.read_text())
        self.assertNotIn("LoadCredentialEncrypted", unit.read_text())
        for path in (unit, directory / "credential.cred", directory / "installation.json", self.state / "config.json"):
            self.assertNotIn(KEY.encode(), path.read_bytes())

    def test_helper_missing_fails_before_privileges_or_key_prompt(self):
        self.helper.side_effect = RuntimeError("Install the matching package before connecting.")
        with self.assertRaisesRegex(RuntimeError, "Install"):
            autostart.enable(self.state)
        self.prompt.assert_not_called()
        self.assertFalse(any(args[0] == "sudo" for args, _ in self.calls))
        self.assertEqual([], self.system_calls)

    def test_node_255_startup_does_not_need_a_credential_backend(self):
        config.save_config(self.state, config.default_config(role="node"))
        self.helper.side_effect = RuntimeError("missing helper")
        # The absent-helper case must not depend on another package being
        # installed on the machine running this source-checkout test.
        with mock.patch.object(autostart, "_helper_present", return_value=False):
            autostart._enable(self.state, confirm=False, start_now=False)
        self.helper.assert_not_called(); self.prompt.assert_not_called()
        self.assertFalse(self.operations("protect"))
        self.assertFalse(any(args[:2] == ["systemd-creds", "--version"] for args, _ in self.calls))
        self.assertTrue(autostart._manifest(autostart._paths(self.state)[1])["node"])

    def test_255_decrypt_is_owner_bound_by_helper_not_owner_supplied_cipher(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        self.calls.clear()
        self.assertEqual(KEY, autostart._credential(self.state))
        args, options = self.operations("decrypt")[0]
        record = autostart._manifest(autostart._paths(self.state)[1])
        self.assertEqual(["sudo", "-n", "--", str(autostart.HELPER), "decrypt", autostart._profile_id(self.state), record["credential_sha256"]], args)
        self.assertNotIn("data", options)
        self.assertFalse(any(a.startswith("--uid") for a in args))

    def test_legacy_manifest_keeps_256_decryption_without_autodetection(self):
        _, directory, _ = autostart._paths(self.state)
        cipher = b"LEGACY-CIPHERTEXT-" * 8
        autostart._write(directory / "credential.cred", cipher)
        autostart._json(directory / "installation.json", {"schema": 1, "node": False, "credential_sha256": hashlib.sha256(cipher).hexdigest()})
        self.assertEqual(KEY, autostart._credential(self.state))
        args, options = self.operations("decrypt")[0]
        self.assertEqual(["systemd-creds", "decrypt", "--user", "--name=chatgpt-runtime-key", "-", "-"], args)
        self.assertEqual(cipher, options["data"])
        self.helper.assert_not_called()

    def test_changed_cipher_fails_before_any_privileged_decrypt(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        (autostart._paths(self.state)[1] / "credential.cred").write_bytes(b"tampered")
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            autostart._credential(self.state)
        self.assertEqual([], self.calls)

    def test_failed_service_setup_revokes_new_helper_grant(self):
        def fail(*args, **kwargs):
            if args[0] == "enable": raise RuntimeError("synthetic service failure")
            return self.fake_systemctl(*args, **kwargs)
        self.systemctl.side_effect = fail
        with self.assertRaises(RuntimeError):
            autostart.configure_connection(self.state, self.settings, KEY)
        self.assertEqual(1, len(self.operations("revoke")))
        self.assertIsNone(autostart._manifest(autostart._paths(self.state)[1]))

    def test_input_race_after_protect_revokes_before_stopping_any_service(self):
        def changed(args, **kwargs):
            result = self.fake_command(args, **kwargs)
            if "protect" in args:
                settings = config.load_config(self.state); settings["name"] = "changed-during-protect"
                config.save_config(self.state, settings)
            return result
        self.command.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, "changed during"):
            autostart.configure_connection(self.state, self.settings, KEY)
        self.assertEqual(1, len(self.operations("revoke")))
        self.assertFalse(any(args[0] == "stop" for args in self.system_calls))

    def test_rotation_revokes_old_grant_only_after_commit(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        directory = autostart._paths(self.state)[1]
        old = autostart._manifest(directory)["credential_sha256"]
        self.calls.clear(); self.system_calls.clear()
        autostart.configure_connection(self.state, self.settings, KEY)
        current = autostart._manifest(directory)
        self.assertNotEqual(old, current["credential_sha256"])
        self.assertEqual([], current["retired_helper_credentials"])
        self.assertEqual(old, self.operations("revoke")[0][0][-1])

    def test_retired_limit_rejects_rotation_before_protect_or_service_change(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        directory = autostart._paths(self.state)[1]
        record = autostart._manifest(directory)
        record["retired_helper_credentials"] = [format(n, "064x") for n in range(128)]
        autostart._json(directory / "installation.json", record, replace=True)
        before = (directory / "installation.json").read_bytes()
        self.calls.clear(); self.system_calls.clear()
        with self.assertRaisesRegex(RuntimeError, "must be cleaned"):
            autostart.configure_connection(self.state, self.settings, KEY)
        self.assertFalse(self.operations("protect"))
        self.assertEqual([], self.system_calls)
        self.assertEqual(before, (directory / "installation.json").read_bytes())

    def test_retirement_failure_is_durable_and_disable_revokes_both_versions(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        directory = autostart._paths(self.state)[1]
        old = autostart._manifest(directory)["credential_sha256"]
        def fail_revoke(args, **kwargs):
            if "revoke" in args: raise RuntimeError("synthetic revocation failure")
            return self.fake_command(args, **kwargs)
        self.command.side_effect = fail_revoke
        autostart.configure_connection(self.state, self.settings, KEY)
        record = autostart._manifest(directory)
        self.assertEqual([old], record["retired_helper_credentials"])
        self.command.side_effect = self.fake_command; self.calls.clear()
        autostart.disable(self.state)
        self.assertEqual("-", self.operations("prune")[0][0][-1])
        self.assertIsNone(autostart._manifest(directory))

    def test_failed_revoke_keeps_manifest_for_retry_after_service_stop(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        directory = autostart._paths(self.state)[1]
        def fail_revoke(args, **kwargs):
            if "prune" in args: raise RuntimeError("synthetic revocation failure")
            return self.fake_command(args, **kwargs)
        self.command.side_effect = fail_revoke
        with self.assertRaises(RuntimeError): autostart.disable(self.state)
        self.assertIsNotNone(autostart._manifest(directory))
        self.assertTrue(autostart._paths(self.state)[2].exists())

    def test_disable_recovers_protect_without_an_owner_manifest(self):
        installed = self.home / "installed-helper"
        installed.write_text("synthetic trusted helper placeholder")
        with mock.patch.object(autostart, "HELPER", installed):
            result = autostart.disable(self.state)
        self.assertTrue(result["orphan_credential_cleanup_verified"])
        self.assertEqual("-", self.operations("prune")[0][0][-1])
        self.assertTrue(any(args[:2] == ["sudo", "-v"] for args, _ in self.calls))

    def test_disable_without_installed_helper_does_not_claim_verified_cleanup(self):
        with mock.patch.object(autostart, "HELPER", self.home / "missing-helper"):
            result = autostart.disable(self.state)
        self.assertFalse(result["orphan_credential_cleanup_verified"])

    def test_256_setup_and_disable_reconcile_orphans_after_os_upgrade(self):
        installed = self.home / "installed-helper"
        installed.write_text("synthetic trusted helper placeholder")
        def systemd256(args, **kwargs):
            if args[:2] == ["systemd-creds", "--version"]:
                self.calls.append((args, kwargs))
                return subprocess.CompletedProcess(args, 0, b"systemd 256\n", b"")
            return self.fake_command(args, **kwargs)
        self.command.side_effect = systemd256
        with mock.patch.object(autostart, "HELPER", installed):
            autostart.configure_connection(self.state, self.settings, KEY)
            self.assertEqual(1, len(self.operations("prune")))
            self.assertTrue(self.operations("encrypt")); self.assertFalse(self.operations("protect"))
            autostart.disable(self.state)
        self.assertEqual(2, len(self.operations("prune")))

    def test_unknown_backend_never_invokes_helper_or_decrypt(self):
        _, directory, _ = autostart._paths(self.state)
        cipher = b"CIPHERTEXT-" * 10
        autostart._write(directory / "credential.cred", cipher)
        autostart._json(directory / "installation.json", {"schema": 1, "node": False, "credential_backend": "unexpected", "credential_sha256": hashlib.sha256(cipher).hexdigest()})
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            autostart._credential(self.state)
        self.assertEqual([], self.calls)

    def test_unsafe_or_ambiguous_policy_never_receives_secret_input(self):
        for backend in (autostart.HOST_BACKEND, autostart.USER_BACKEND):
            for kind in ("logging", "missing", "ambiguous", "different-command", "query-failed", "specific-stream-logging"):
                self.calls.clear()
                def policy(args, **kwargs):
                    result = self.fake_command(args, **kwargs)
                    if "-ll" not in args:
                        return result
                    text = result.stdout.decode()
                    if kind == "logging": text = text.replace("!log_input", "log_input")
                    elif kind == "missing": text = text.replace("!log_output, ", "")
                    elif kind == "ambiguous": text += "Matched: /usr/bin/another\n"
                    elif kind == "different-command": text = text.replace(" Matched: ", " Matched: /different ")
                    elif kind == "specific-stream-logging": text = text.replace("!setenv", "!setenv, log_stdin")
                    return subprocess.CompletedProcess(args, 1 if kind == "query-failed" else 0, text.encode(), b"")
                self.command.side_effect = policy
                with self.subTest(backend=backend, kind=kind), self.assertRaises(RuntimeError):
                    autostart._encrypt_credential(self.state, KEY, backend)
                self.assertFalse(any(options.get("data") == KEY.encode() for _, options in self.calls))
                self.assertTrue(self.operations("authorize"))
                self.assertTrue(self.operations("unauthorize"))

    def test_policy_query_exception_precedes_secret_and_cancels_authorization(self):
        def unavailable(args, **kwargs):
            if "-ll" in args:
                raise RuntimeError("synthetic policy lookup failure")
            return self.fake_command(args, **kwargs)
        self.command.side_effect = unavailable
        with self.assertRaises(RuntimeError):
            autostart._encrypt_credential(self.state, KEY, autostart.HOST_BACKEND)
        self.assertFalse(any(options.get("data") == KEY.encode() for _, options in self.calls))
        self.assertTrue(self.operations("unauthorize"))

    def test_decrypt_checks_effective_nolog_policy_before_running_privileged_command(self):
        autostart.configure_connection(self.state, self.settings, KEY)
        self.calls.clear()
        def logging(args, **kwargs):
            result = self.fake_command(args, **kwargs)
            if "-ll" in args:
                return subprocess.CompletedProcess(args, 0, result.stdout.replace(b"!log_output", b"log_output"), b"")
            return result
        self.command.side_effect = logging
        with self.assertRaisesRegex(RuntimeError, "explicitly disable"):
            autostart._credential(self.state)
        self.assertFalse(self.operations("decrypt"))


if __name__ == "__main__":
    unittest.main()
