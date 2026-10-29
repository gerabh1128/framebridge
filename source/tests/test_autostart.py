import fcntl
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from framebridge_expanded import autostart, config, runtime, connection_background, credential_helper

ID = "tunnel_0123456789abcdef0123456789abcdef"
MARKER = "FRAMEBRIDGE-SYNTHETIC-NOT-A-REAL-KEY-AUTOSTART"


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.home / "profile"
        self.settings = config.default_config()
        self.settings.update(tunnel_id=ID, headless="no")
        config.save_config(self.state, self.settings)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Path, "home", return_value=self.home).start()
        self.calls = []
        self.system_calls = []
        self.command = mock.patch.object(autostart, "_command", side_effect=self.fake_command).start()
        mock.patch.object(autostart, "_systemctl", side_effect=self.fake_systemctl).start()
        mock.patch.object(autostart, "_linger", return_value=False).start()
        mock.patch.object(autostart, "_enabled_units", return_value=[]).start()
        mock.patch.object(autostart, "_helper_ready").start()
        mock.patch.object(autostart.os, "geteuid", return_value=1000).start()
        mock.patch.object(autostart.resource, "setrlimit").start()
        self.inputs = mock.patch("builtins.input", side_effect=["y"]).start()
        self.key_prompt = mock.patch.object(autostart.getpass, "getpass", return_value=MARKER).start()
        mock.patch.object(autostart, "status", return_value={"configured": True}).start()
        mock.patch("sys.stdout", new_callable=io.StringIO).start()

    def fake_command(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if "-ll" in args:
            command = " ".join(args[args.index("--") + 1:])
            output = "Sudoers entry: /etc/sudoers.d/synthetic-test\n RunAsUsers: root\n Options: !authenticate, !log_input, !log_output, !setenv\n Commands:\n  " + command + "\n Matched: " + command + "\n"
            return subprocess.CompletedProcess(args, 0, output.encode(), b"")
        if args[:2] == ["systemd-creds", "--version"]:
            return subprocess.CompletedProcess(args, 0, b"systemd 257\n", b"")
        if "encrypt" in args and "-ll" not in args:
            return subprocess.CompletedProcess(args, 0, b"CIPHERTEXT-ONLY-" * 20, b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def fake_systemctl(self, *args, **kwargs):
        self.system_calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def test_enable_passes_key_only_by_anonymous_input_and_enables_boot_fallback(self):
        before = (self.state / "config.json").read_bytes()
        autostart.enable(self.state)
        state, directory, unit = autostart._paths(self.state)
        self.assertNotEqual(before, (self.state / "config.json").read_bytes())
        self.assertEqual("auto", config.load_config(self.state)["headless"])
        encryption = [(a, kw) for a, kw in self.calls if "encrypt" in a and "-ll" not in a]
        self.assertEqual(1, len(encryption))
        args, options = encryption[0]
        self.assertEqual(MARKER.encode(), options["data"])
        self.assertNotIn(MARKER, " ".join(args))
        self.assertIn("--with-key=host", args)
        self.assertIn("--uid=" + str(os.getuid()), args)
        for path in [directory / "credential.cred", directory / "installation.json", unit]:
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertNotIn(MARKER.encode(), path.read_bytes())
        self.assertNotIn("LoadCredentialEncrypted=", unit.read_text())
        self.assertIn("StandardInput=null", unit.read_text())
        self.assertIn(("enable", "--now", unit.name), self.system_calls)
        autostart.enable(self.state)
        self.assertEqual(1, self.key_prompt.call_count)
        self.inputs.assert_called_once_with("Enable automatic startup? [y/N]: ")

    def test_boot_fallback_restores_only_unchanged_profile_on_disable(self):
        self.inputs.side_effect = ["y"]
        autostart.enable(self.state)
        self.assertEqual("auto", config.load_config(self.state)["headless"])
        result = autostart.disable(self.state)
        self.assertTrue(result["desktop_setting_restored"])
        self.assertTrue(result["linger_restored"])
        self.assertEqual("no", config.load_config(self.state)["headless"])
        _, directory, unit = autostart._paths(self.state)
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "credential.cred").exists())

    def test_disable_keeps_desktop_changes_made_after_enable(self):
        self.inputs.side_effect = ["y"]
        autostart.enable(self.state)
        settings = config.load_config(self.state)
        settings["name"] = "changed-later"
        config.save_config(self.state, settings)
        result = autostart.disable(self.state)
        self.assertFalse(result["desktop_setting_restored"])
        self.assertEqual("changed-later", config.load_config(self.state)["name"])
        self.assertEqual("auto", config.load_config(self.state)["headless"])

    def test_modified_unit_is_not_stopped_or_removed(self):
        autostart.enable(self.state)
        _, _, unit = autostart._paths(self.state)
        unit.write_text(unit.read_text() + "# external edit\n")
        self.system_calls.clear()
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            autostart.disable(self.state)
        self.assertEqual([], self.system_calls)
        self.assertTrue(unit.exists())

    def test_encryption_failure_never_creates_unit_or_disables_an_existing_service(self):
        def failing(args, **kwargs):
            if "encrypt" in args and "-ll" not in args:
                raise RuntimeError("Credential encryption failed.")
            return self.fake_command(args, **kwargs)
        self.command.side_effect = failing
        with self.assertRaisesRegex(RuntimeError, "encryption failed"):
            autostart.enable(self.state)
        _, directory, unit = autostart._paths(self.state)
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "credential.cred").exists())
        self.assertFalse(any(x[0] == "disable" for x in self.system_calls))

    def test_cancel_never_requests_credentials_or_privileges(self):
        self.inputs.side_effect = ["cancel"]
        result = autostart.enable(self.state)
        self.assertEqual("cancelled", result["state"])
        self.key_prompt.assert_not_called()
        self.assertFalse(any(a[0] == "sudo" for a, _ in self.calls))

    def test_default_no_keeps_profile_unchanged_without_privileges_or_key(self):
        before = (self.state / "config.json").read_bytes()
        self.inputs.side_effect = [""]
        result = autostart.enable(self.state)
        self.assertEqual({"configured": False, "state": "cancelled"}, result)
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.key_prompt.assert_not_called()
        self.assertFalse(any(args[0] == "sudo" for args, _ in self.calls))
        self.assertEqual([], self.system_calls)
        _, directory, unit = autostart._paths(self.state)
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "credential.cred").exists())

    def test_natural_confirmation_is_case_insensitive_and_requested_once(self):
        self.inputs.side_effect = [" YeS "]
        autostart.enable(self.state)
        autostart.enable(self.state)
        self.inputs.assert_called_once_with("Enable automatic startup? [y/N]: ")
        self.key_prompt.assert_called_once_with("Connection key (hidden): ")

    def test_setup_shows_bounded_progress_without_claiming_connected(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            autostart.enable(self.state)
        text = output.getvalue()
        self.assertLess(text.index("[1/3]"), text.index("[2/3]"))
        self.assertLess(text.index("[2/3]"), text.index("[3/3]"))
        self.assertLessEqual(len(text.splitlines()), 6)
        self.assertTrue(all(len(line) <= 80 for line in text.splitlines()))
        self.assertNotIn(MARKER, text)
        self.assertNotIn("connected", text.lower())
        self.assertNotIn("systemd", text)
        self.assertNotIn("physical-only", text)
        self.assertNotIn("Root access", text)

    def test_eof_or_interrupt_at_confirmation_cancels_without_authentication(self):
        for cancellation in (EOFError, KeyboardInterrupt):
            with self.subTest(cancellation=cancellation.__name__):
                self.inputs.side_effect = cancellation
                result = autostart.enable(self.state)
                self.assertEqual("cancelled", result["state"])
                self.assertFalse(result["configured"])
        self.key_prompt.assert_not_called()
        self.assertFalse(any(args[0] == "sudo" for args, _ in self.calls))
        self.assertEqual([], self.system_calls)

    def _durable_settings(self, identifier=ID):
        return {"account_label": "owner@example.test", "tunnel_id": identifier,
                "control_plane_organization_id": "", "control_plane_url_path": ""}

    def _durable_connect(self, key=MARKER, settings=None):
        with mock.patch.object(connection_background, "status", return_value={"alive": False}), mock.patch.object(connection_background, "disconnect", return_value={"ok": True, "alive": False}):
            return autostart.configure_connection(self.state, settings or self._durable_settings(), key)

    def test_durable_connection_uses_supplied_key_once_without_extra_questions(self):
        self._durable_connect()
        self.inputs.assert_not_called()
        self.key_prompt.assert_not_called()
        encryptions = [(args, kw) for args, kw in self.calls if "encrypt" in args and "-ll" not in args]
        self.assertEqual(1, len(encryptions))
        self.assertEqual(MARKER.encode(), encryptions[0][1]["data"])
        self.assertNotIn(MARKER, " ".join(encryptions[0][0]))
        _, directory, unit = autostart._paths(self.state)
        for path in (self.state / "config.json", directory / "credential.cred", directory / "installation.json", unit):
            self.assertNotIn(MARKER.encode(), path.read_bytes())
        self.assertEqual("auto", config.load_config(self.state)["headless"])
        self.assertIn(("enable", "--now", unit.name), self.system_calls)

    def test_interrupt_reading_status_after_commit_keeps_enabled_service_and_reports_it(self):
        with mock.patch.object(autostart, "status", side_effect=KeyboardInterrupt):
            result = self._durable_connect()
        self.assertEqual({"configured": True, "enabled": True, "interrupted_after_commit": True}, result)
        _, directory, unit = autostart._paths(self.state)
        self.assertTrue(unit.exists())
        self.assertTrue((directory / "credential.cred").exists())
        self.assertTrue((directory / "installation.json").exists())
        self.assertIn(("enable", "--now", unit.name), self.system_calls)
        self.assertNotIn(("disable", "--now", unit.name), self.system_calls)

    def test_durable_connection_can_create_default_profile_in_the_transaction(self):
        (self.state / "config.json").unlink()
        self._durable_connect()
        selected = config.load_config(self.state)
        self.assertEqual("standalone", selected["role"])
        self.assertEqual("auto", selected["headless"])
        self.assertEqual("owner@example.test", selected["account_label"])
        self.assertEqual(ID, selected["tunnel_id"])

    def test_durable_encryption_failure_leaves_current_profile_and_service_alone(self):
        original = (self.state / "config.json").read_bytes()
        def failed(args, **kwargs):
            if "encrypt" in args and "-ll" not in args:
                raise RuntimeError("Protection failed.")
            return self.fake_command(args, **kwargs)
        self.command.side_effect = failed
        with mock.patch.object(connection_background, "disconnect") as disconnect:
            with self.assertRaisesRegex(RuntimeError, "Protection failed"):
                self._durable_connect()
        self.assertEqual(original, (self.state / "config.json").read_bytes())
        self.assertFalse(any(args[0] in {"stop", "enable", "disable"} for args in self.system_calls))
        _, directory, unit = autostart._paths(self.state)
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "credential.cred").exists())

    def test_durable_replacement_encrypts_before_stop_and_replaces_public_identity(self):
        self._durable_connect()
        events = []
        original_command = self.fake_command
        def command(args, **kwargs):
            if "encrypt" in args and "-ll" not in args:
                events.append("encrypt")
                return subprocess.CompletedProcess(args, 0, b"REPLACEMENT-CIPHERTEXT-" * 20, b"")
            return original_command(args, **kwargs)
        def service(*args, **kwargs):
            events.append(args[0])
            if args[0] == "show":
                return subprocess.CompletedProcess(args, 0, b"ActiveState=active\nUnitFileState=enabled\n", b"")
            return self.fake_systemctl(*args, **kwargs)
        live = {"alive": True}
        def disconnect(state):
            events.append("disconnect")
            live["alive"] = False
            return {"ok": True, "alive": False}
        self.command.side_effect = command
        new_id = "tunnel_replacement0123456789"
        with mock.patch.object(autostart, "_systemctl", side_effect=service), mock.patch.object(connection_background, "status", side_effect=lambda state: dict(live)), mock.patch.object(connection_background, "disconnect", side_effect=disconnect):
            autostart.configure_connection(self.state, self._durable_settings(new_id), MARKER + "-NEW")
        self.assertLess(events.index("encrypt"), events.index("stop"))
        self.assertLess(events.index("disconnect"), events.index("enable"))
        self.assertEqual(new_id, config.load_config(self.state)["tunnel_id"])
        _, directory, _ = autostart._paths(self.state)
        self.assertEqual(b"REPLACEMENT-CIPHERTEXT-" * 20, (directory / "credential.cred").read_bytes())

    def test_durable_replacement_failure_restores_exact_files_and_restarts_old_unit(self):
        self._durable_connect()
        _, directory, unit = autostart._paths(self.state)
        paths = (self.state / "config.json", unit, directory / "credential.cred", directory / "installation.json", directory / "config-before.json")
        originals = {p: p.read_bytes() for p in paths}
        calls = []
        def failed(*args, **kwargs):
            calls.append(args)
            if args[0] == "show":
                return subprocess.CompletedProcess(args, 0, b"ActiveState=active\nUnitFileState=enabled\n", b"")
            if args[:2] == ("enable", "--now"):
                raise RuntimeError("New service did not start.")
            return self.fake_systemctl(*args, **kwargs)
        with mock.patch.object(autostart, "_systemctl", side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, "New service did not start"):
                self._durable_connect(MARKER + "-NEW", self._durable_settings("tunnel_changed0123456789"))
        self.assertEqual(originals, {p: p.read_bytes() for p in paths})
        self.assertIn(("start", unit.name), calls)
        self.assertIn(("enable", unit.name), calls)

    def test_durable_connection_rejects_changed_existing_service_before_authentication(self):
        self._durable_connect()
        _, _, unit = autostart._paths(self.state)
        unit.write_bytes(unit.read_bytes() + b"# external change\n")
        self.calls.clear()
        self.system_calls.clear()
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            self._durable_connect(MARKER + "-NEW")
        self.assertEqual([], self.calls)
        self.assertEqual([], self.system_calls)

    def test_durable_invalid_key_or_public_fields_have_no_authentication_side_effect(self):
        for key, settings in (("bad key", self._durable_settings()), (MARKER + "é", self._durable_settings()), (MARKER, {**self._durable_settings(), "private_key": "not-accepted"})):
            with self.subTest(key_length=len(key)):
                with self.assertRaises(ValueError):
                    self._durable_connect(key, settings)
        self.assertEqual([], self.calls)
        self.assertEqual([], self.system_calls)

    def test_durable_failure_reports_previous_manual_connection_needs_reconnecting(self):
        live = {"alive": True}
        def disconnect(state):
            live["alive"] = False
            return {"ok": True, "alive": False}
        def failed(*args, **kwargs):
            if args[:2] == ("enable", "--now"):
                raise RuntimeError("Start failed.")
            return self.fake_systemctl(*args, **kwargs)
        before = (self.state / "config.json").read_bytes()
        with mock.patch.object(connection_background, "status", side_effect=lambda state: dict(live)), mock.patch.object(connection_background, "disconnect", side_effect=disconnect), mock.patch.object(autostart, "_systemctl", side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, "previous manually started connection was stopped"):
                autostart.configure_connection(self.state, self._durable_settings(), MARKER)
        self.assertEqual(before, (self.state / "config.json").read_bytes())

    def test_durable_rotation_then_disable_preserves_new_connection_settings(self):
        self._durable_connect()
        replacement = self._durable_settings("tunnel_next0123456789")
        replacement["account_label"] = "new@example.test"
        self._durable_connect(MARKER + "-NEW", replacement)
        autostart.disable(self.state)
        selected = config.load_config(self.state)
        self.assertEqual("no", selected["headless"])
        self.assertEqual(replacement["tunnel_id"], selected["tunnel_id"])
        self.assertEqual(replacement["account_label"], selected["account_label"])

    def test_durable_rollback_attempts_old_restart_even_after_disable_failure(self):
        self._durable_connect()
        _, _, unit = autostart._paths(self.state)
        calls = []
        def failed(*args, **kwargs):
            calls.append(args)
            if args[0] == "show":
                return subprocess.CompletedProcess(args, 0, b"ActiveState=active\nUnitFileState=enabled\n", b"")
            if args[:2] == ("enable", "--now"):
                raise RuntimeError("Start failed.")
            return subprocess.CompletedProcess(args, 1 if args[:2] == ("disable", "--now") else 0, b"", b"")
        with mock.patch.object(autostart, "_systemctl", side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, "restoration could not be fully verified"):
                self._durable_connect(MARKER + "-NEW")
        self.assertIn(("start", unit.name), calls)
        self.assertIn(("enable", unit.name), calls)

    def test_resume_saved_stopped_connection_asks_for_no_key_or_privilege_when_linger_exists(self):
        self._durable_connect()
        _, directory, unit = autostart._paths(self.state)
        old_cipher = (directory / "credential.cred").read_bytes()
        old_unit = unit.read_bytes()
        self.calls.clear()
        self.system_calls.clear()
        with mock.patch.object(autostart, "_linger", return_value=True), mock.patch.object(connection_background, "disconnect", return_value={"ok": True, "alive": False}), mock.patch.object(connection_background, "status", return_value={"alive": False}):
            autostart.resume_connection(self.state)
        self.inputs.assert_not_called()
        self.key_prompt.assert_not_called()
        self.assertEqual([], self.calls)
        self.assertEqual(old_cipher, (directory / "credential.cred").read_bytes())
        self.assertEqual(old_unit, unit.read_bytes())
        self.assertIn(("enable", "--now", unit.name), self.system_calls)

    def test_resume_active_verified_service_returns_without_restarting(self):
        self._durable_connect()
        self.calls.clear()
        self.system_calls.clear()
        ready = {"configured": True, "enabled": True, "service_active": True, "runtime": {"running": True, "ready": True}, "connection": {"alive": True, "verified": True}}
        with mock.patch.object(autostart, "_linger", return_value=True), mock.patch.object(autostart, "status", return_value=ready):
            self.assertEqual(ready, autostart.resume_connection(self.state))
        self.assertEqual([], self.calls)
        self.assertEqual([], self.system_calls)

    def test_resume_rejects_public_tunnel_change_before_service_actions(self):
        self._durable_connect()
        changed = config.load_config(self.state)
        changed["tunnel_id"] = "tunnel_changed0123456789"
        config.save_config(self.state, changed)
        self.calls.clear()
        self.system_calls.clear()
        with self.assertRaisesRegex(RuntimeError, "--replace"):
            autostart.resume_connection(self.state)
        self.assertEqual([], self.calls)
        self.assertEqual([], self.system_calls)

    def test_resume_rejects_changed_cipher_before_service_actions(self):
        self._durable_connect()
        _, directory, _ = autostart._paths(self.state)
        cipher = directory / "credential.cred"
        cipher.write_bytes(cipher.read_bytes() + b"changed")
        self.calls.clear()
        self.system_calls.clear()
        with self.assertRaisesRegex(RuntimeError, "--replace"):
            autostart.resume_connection(self.state)
        self.assertEqual([], self.calls)
        self.assertEqual([], self.system_calls)

    def test_resume_legacy_without_any_profile_binding_requires_replace(self):
        self._durable_connect()
        _, directory, _ = autostart._paths(self.state)
        record = autostart._manifest(directory)
        record.pop("connection_settings_sha256")
        record.pop("changed_config_sha256")
        autostart._json(directory / "installation.json", record, replace=True)
        self.calls.clear()
        self.system_calls.clear()
        with self.assertRaisesRegex(RuntimeError, "--replace"):
            autostart.resume_connection(self.state)
        self.assertEqual([], self.calls)
        self.assertEqual([], self.system_calls)

    def test_resume_legacy_exact_profile_binding_can_be_verified_without_new_key(self):
        self._durable_connect()
        _, directory, _ = autostart._paths(self.state)
        record = autostart._manifest(directory)
        record.pop("connection_settings_sha256")
        autostart._json(directory / "installation.json", record, replace=True)
        ready = {"configured": True, "enabled": True, "service_active": True, "runtime": {"running": True, "ready": True}, "connection": {"alive": True, "verified": True}}
        with mock.patch.object(autostart, "_linger", return_value=True), mock.patch.object(autostart, "status", return_value=ready):
            self.assertEqual(ready, autostart.resume_connection(self.state))
        self.key_prompt.assert_not_called()

    def test_node_autostart_needs_no_chatgpt_key(self):
        settings = config.default_config(role="node")
        settings.update(headless="yes", broker_url="https://example.test:20878")
        config.save_config(self.state, settings)
        self.inputs.side_effect = ["yes"]
        autostart.enable(self.state)
        _, directory, unit = autostart._paths(self.state)
        self.key_prompt.assert_not_called()
        self.assertNotIn("LoadCredentialEncrypted", unit.read_text())
        self.assertFalse((directory / "credential.cred").exists())

    def test_fresh_node_with_existing_linger_authenticates_before_existing_grant_cleanup(self):
        for start_now in (False, True):
            with self.subTest(start_now=start_now):
                state = self.home / ("node-now" if start_now else "node-boot")
                config.save_config(state, config.default_config(role="node"))
                before = (state / "config.json").read_bytes()
                self.calls.clear()
                self.system_calls.clear()
                with mock.patch.object(autostart, "_linger", return_value=True), mock.patch.object(autostart, "_helper_present", return_value=True), mock.patch.object(autostart, "_node_helper_cleanup_required", return_value=True), mock.patch.object(autostart, "_credential_backend", side_effect=AssertionError("A node has no connection credential")):
                    autostart._enable(state, start_now=start_now, confirm=False)
                _, directory, unit = autostart._paths(state)
                expected_prune = ["sudo", "-n", "--", str(autostart.HELPER), "prune", autostart._profile_id(state), "-"]
                # Exercise the real prune implementation through the command
                # boundary: no authentication or global boot change is hidden.
                self.assertEqual([["sudo", "-v"], expected_prune], [args for args, _ in self.calls])
                self.assertEqual(before, (state / "config.json").read_bytes())
                self.assertTrue(autostart._manifest(directory)["linger_before"])
                self.assertTrue(autostart._manifest(directory)["node"])
                self.assertFalse((directory / "credential.cred").exists())
                self.assertIn(("enable", "--now", unit.name) if start_now else ("enable", unit.name), self.system_calls)
                self.assertEqual(0o600, stat.S_IMODE(unit.stat().st_mode))
                self.inputs.assert_not_called()
                self.key_prompt.assert_not_called()

    def test_fresh_node_with_existing_linger_fails_closed_when_prune_is_not_authorized(self):
        config.save_config(self.state, config.default_config(role="node"))
        before = (self.state / "config.json").read_bytes()

        def denied_prune(args, **kwargs):
            if len(args) > 4 and args[:4] == ["sudo", "-n", "--", str(autostart.HELPER)] and args[4] == "prune":
                self.calls.append((args, kwargs))
                raise RuntimeError("Synthetic cleanup grant denied")
            return self.fake_command(args, **kwargs)

        self.command.side_effect = denied_prune
        with mock.patch.object(autostart, "_linger", return_value=True), mock.patch.object(autostart, "_helper_present", return_value=True), mock.patch.object(autostart, "_node_helper_cleanup_required", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "cleanup grant denied"):
                autostart._enable(self.state, confirm=False)
        _, directory, unit = autostart._paths(self.state)
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "installation.json").exists())
        self.assertEqual([], self.system_calls)
        self.assertEqual(["sudo", "-v"], self.calls[0][0])
        self.assertFalse(any("loginctl" in args for args, _ in self.calls))

    def test_fresh_node_with_existing_linger_and_no_managed_grants_needs_no_privilege(self):
        policies = self.root / "policies"
        policies.mkdir()
        profile = autostart._profile_id(self.state)
        # Other profiles and owners must not cause an unnecessary request.
        (policies / ("framebridge-credential-" + str(os.getuid()) + "-" + "a" * 64 + "-" + "b" * 64)).touch()
        (policies / ("framebridge-credential-987654321-" + profile + "-" + "b" * 64)).touch()
        config.save_config(self.state, config.default_config(role="node"))
        before = (self.state / "config.json").read_bytes()
        with mock.patch.object(autostart, "_linger", return_value=True), mock.patch.object(autostart, "_helper_present", return_value=True), mock.patch.object(credential_helper, "SUDOERS", policies), mock.patch.object(credential_helper, "_root_path", side_effect=lambda path, **kw: path):
            autostart._enable(self.state, confirm=False, start_now=False)
        _, directory, unit = autostart._paths(self.state)
        self.assertEqual([], self.calls)
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.assertIn(("enable", unit.name), self.system_calls)
        self.assertTrue(autostart._manifest(directory)["node"])
        self.assertFalse((directory / "credential.cred").exists())
        self.key_prompt.assert_not_called()

    def test_ambiguous_node_policy_fails_before_authentication_or_managed_writes(self):
        policies = self.root / "policies"
        policies.mkdir()
        name = "framebridge-credential-authorize-" + str(os.getuid()) + "-" + autostart._profile_id(self.state) + "-unknown"
        (policies / name).touch()
        config.save_config(self.state, config.default_config(role="node"))
        before = (self.state / "config.json").read_bytes()
        with mock.patch.object(autostart, "_linger", return_value=True), mock.patch.object(autostart, "_helper_present", return_value=True), mock.patch.object(credential_helper, "SUDOERS", policies), mock.patch.object(credential_helper, "_root_path", side_effect=lambda path, **kw: path):
            with self.assertRaisesRegex(RuntimeError, "ambiguous.*administrator review"):
                autostart._enable(self.state, confirm=False)
        _, directory, unit = autostart._paths(self.state)
        self.assertEqual([], self.calls)
        self.assertEqual([], self.system_calls)
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "installation.json").exists())
        self.assertFalse((directory / "credential.cred").exists())
        self.assertTrue((policies / name).exists())

    def test_fresh_node_without_linger_requires_authentication_before_managed_writes(self):
        config.save_config(self.state, config.default_config(role="node"))
        before = (self.state / "config.json").read_bytes()

        def denied_authentication(args, **kwargs):
            if args == ["sudo", "-v"]:
                self.calls.append((args, kwargs))
                raise RuntimeError("Synthetic owner authentication required")
            return self.fake_command(args, **kwargs)

        self.command.side_effect = denied_authentication
        with mock.patch.object(autostart, "_helper_present", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "owner authentication required"):
                autostart._enable(self.state, confirm=False)
        _, directory, unit = autostart._paths(self.state)
        self.assertEqual([["sudo", "-v"]], [args for args, _ in self.calls])
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "installation.json").exists())
        self.assertFalse((directory / "credential.cred").exists())
        self.assertEqual([], self.system_calls)
        self.key_prompt.assert_not_called()

    def test_fresh_node_without_linger_keeps_administrative_boot_setup(self):
        config.save_config(self.state, config.default_config(role="node"))
        with mock.patch.object(autostart, "_helper_present", return_value=True):
            autostart._enable(self.state, confirm=False)
        user = autostart.pwd.getpwuid(os.getuid()).pw_name
        expected_prune = ["sudo", "-n", "--", str(autostart.HELPER), "prune", autostart._profile_id(self.state), "-"]
        self.assertEqual([["sudo", "-v"], expected_prune,
                          ["sudo", "-n", "--", "loginctl", "enable-linger", user]],
                         [args for args, _ in self.calls])
        self.key_prompt.assert_not_called()

    def test_central_with_existing_linger_still_authenticates_and_protects_its_key(self):
        settings = config.default_config(role="central")
        settings.update(tunnel_id=ID, headless="auto")
        config.save_config(self.state, settings)
        with mock.patch.object(autostart, "_linger", return_value=True):
            autostart.enable(self.state)
        self.assertIn(["sudo", "-v"], [args for args, _ in self.calls])
        encryption = [(args, options) for args, options in self.calls if "encrypt" in args and "-ll" not in args]
        self.assertEqual(1, len(encryption))
        self.assertEqual(MARKER.encode(), encryption[0][1]["data"])
        self.assertNotIn(MARKER, " ".join(encryption[0][0]))
        self.key_prompt.assert_called_once_with("Connection key (hidden): ")
        _, directory, _ = autostart._paths(self.state)
        self.assertTrue((directory / "credential.cred").exists())

    def test_central_existing_linger_does_not_bypass_failed_owner_authentication(self):
        settings = config.default_config(role="central")
        settings.update(tunnel_id=ID, headless="auto")
        config.save_config(self.state, settings)
        before = (self.state / "config.json").read_bytes()

        def denied_authentication(args, **kwargs):
            if args == ["sudo", "-v"]:
                raise RuntimeError("Synthetic owner authentication required")
            return self.fake_command(args, **kwargs)

        self.command.side_effect = denied_authentication
        with mock.patch.object(autostart, "_linger", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "owner authentication required"):
                autostart.enable(self.state)
        _, directory, unit = autostart._paths(self.state)
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "installation.json").exists())
        self.assertFalse((directory / "credential.cred").exists())
        self.key_prompt.assert_not_called()

    def test_private_file_reader_rejects_symlink_hardlink_fifo_and_public_file(self):
        original = self.root / "original"
        original.write_bytes(b"safe")
        original.chmod(0o600)
        linked = self.root / "linked"
        linked.symlink_to(original)
        with self.assertRaises(OSError):
            autostart._read(linked)
        linked.unlink()
        os.link(original, linked)
        with self.assertRaises(RuntimeError):
            autostart._read(original)
        linked.unlink()
        os.mkfifo(linked, 0o600)
        with self.assertRaises(RuntimeError):
            autostart._read(linked)
        original.chmod(0o644)
        with self.assertRaises(RuntimeError):
            autostart._read(original)

    def test_configuration_lock_rejects_concurrent_enable(self):
        _, directory, _ = autostart._paths(self.state)
        fd = os.open(directory / "configuration.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                autostart.enable(self.state)
            self.key_prompt.assert_not_called()
        finally:
            os.close(fd)

    def _service_directory_owners(self, owners):
        actual_lstat = Path.lstat

        def owner_lstat(path, *args, **kwargs):
            values = list(actual_lstat(path, *args, **kwargs))
            values[4] = owners.get(path, values[4])
            return os.stat_result(values)

        return mock.patch.object(Path, "lstat", owner_lstat)

    def test_root_owned_service_ancestors_allow_owned_final_directory_without_changes(self):
        final = self.home / ".config/systemd/user"
        final.mkdir(parents=True, mode=0o700)
        directories = [self.home / ".config", final.parent, final]
        before = {p: (p.stat().st_uid, p.stat().st_mode) for p in directories}
        owners = {directories[0]: 0, directories[1]: 0, final: 1000}
        with mock.patch.object(autostart.os, "getuid", return_value=1000), self._service_directory_owners(owners):
            autostart._check_unit_directory(final / "test.service")
        self.assertEqual(before, {p: (p.stat().st_uid, p.stat().st_mode) for p in directories})
        self.assertEqual([], list(final.iterdir()))

    def test_root_or_other_account_cannot_own_final_service_directory(self):
        final = self.home / ".config/systemd/user"
        final.mkdir(parents=True, mode=0o700)
        for owner in (0, 2000):
            with self.subTest(owner=owner), mock.patch.object(autostart.os, "getuid", return_value=1000), self._service_directory_owners({self.home / ".config": 0, final.parent: 0, final: owner}):
                with self.assertRaisesRegex(RuntimeError, "final directory must belong"):
                    autostart._check_unit_directory(final / "test.service")
        self.assertEqual([], list(final.iterdir()))

    def test_other_account_cannot_own_service_ancestors(self):
        final = self.home / ".config/systemd/user"
        final.mkdir(parents=True, mode=0o700)
        for ancestor in (self.home / ".config", final.parent):
            owners = {self.home / ".config": 0, final.parent: 0, final: 1000, ancestor: 2000}
            with self.subTest(ancestor=ancestor.name), mock.patch.object(autostart.os, "getuid", return_value=1000), self._service_directory_owners(owners):
                with self.assertRaisesRegex(RuntimeError, "safe owner"):
                    autostart._check_unit_directory(final / "test.service")

    def test_root_owned_service_ancestors_still_reject_writable_or_symbolic_directories(self):
        final = self.home / ".config/systemd/user"
        final.mkdir(parents=True, mode=0o700)
        directories = [self.home / ".config", final.parent, final]
        owners = {directories[0]: 0, directories[1]: 0, final: 1000}
        for directory in directories:
            original = directory.stat().st_mode
            for mode in (0o775, 0o777):
                with self.subTest(directory=directory.name, mode=mode):
                    directory.chmod(mode)
                    with mock.patch.object(autostart.os, "getuid", return_value=1000), self._service_directory_owners(owners):
                        with self.assertRaisesRegex(RuntimeError, "not be writable"):
                            autostart._check_unit_directory(final / "test.service")
                    self.assertEqual(mode, stat.S_IMODE(directory.stat().st_mode))
            directory.chmod(stat.S_IMODE(original))
            moved = directory.with_name(directory.name + "-target")
            directory.rename(moved)
            directory.symlink_to(moved, target_is_directory=True)
            try:
                with self.subTest(directory=directory.name, symbolic=True):
                    with self.assertRaisesRegex(RuntimeError, "symbolic links"):
                        autostart._check_unit_directory(final / "test.service")
            finally:
                directory.unlink()
                moved.rename(directory)

    def test_root_owned_parent_creation_failure_preserves_permissions_and_explains_failure(self):
        parent = self.home / ".config/systemd"
        parent.mkdir(parents=True, mode=0o755)
        final = parent / "user"
        before = {p: (p.stat().st_uid, p.stat().st_mode) for p in (parent.parent, parent)}
        actual_mkdir = Path.mkdir

        def denied_final(path, *args, **kwargs):
            if path == final:
                raise PermissionError("Synthetic administrator-owned parent")
            return actual_mkdir(path, *args, **kwargs)

        with mock.patch.object(autostart.os, "getuid", return_value=1000), self._service_directory_owners({parent.parent: 0, parent: 0}), mock.patch.object(Path, "mkdir", denied_final):
            with self.assertRaisesRegex(RuntimeError, "cannot create.*left unchanged"):
                autostart._check_unit_directory(final / "test.service")
        self.assertFalse(final.exists())
        self.assertEqual(before, {p: (p.stat().st_uid, p.stat().st_mode) for p in before})

    def test_unit_quotes_specifiers_and_dollars_without_shell_or_missing_package_loop(self):
        text = autostart.unit_text(Path('/tmp/a % b ${HOME} "quoted"'), Path('/tmp/key % file'))
        self.assertIn("%%", text)
        self.assertIn("$${HOME}", text)
        self.assertIn("ConditionPathExists=", text)
        self.assertIn("LimitCORE=0", text)
        self.assertIn("RestartSec=15", text)
        self.assertNotIn("/bin/sh", text)
        with self.assertRaises(ValueError):
            autostart.unit_text(Path("/tmp/a\nExecStart=bad"), Path("/tmp/key"))

    def test_encrypted_credential_uses_os_broker_pipe_without_plaintext_files(self):
        import hashlib
        _, directory, _ = autostart._paths(self.state)
        cipher = b"SYNTHETIC-CIPHERTEXT-ONLY" * 8
        autostart._write(directory / "credential.cred", cipher)
        autostart._json(directory / "installation.json", {"schema": 1, "node": False, "credential_sha256": hashlib.sha256(cipher).hexdigest()})
        def decrypt(args, **kwargs):
            self.assertEqual(["systemd-creds", "decrypt", "--user", "--name=chatgpt-runtime-key", "-", "-"], args)
            self.assertEqual(cipher, kwargs["data"])
            self.assertEqual(15, kwargs["timeout"])
            self.assertNotIn(MARKER, " ".join(args))
            return subprocess.CompletedProcess(args, 0, MARKER.encode(), b"")
        self.command.side_effect = decrypt
        self.assertEqual(MARKER, autostart._credential(self.state))
        for path in directory.iterdir():
            if path.is_file():
                self.assertNotIn(MARKER.encode(), path.read_bytes())

    def test_changed_cipher_is_rejected_before_decryption(self):
        import hashlib
        _, directory, _ = autostart._paths(self.state)
        cipher = b"SYNTHETIC-CIPHERTEXT-ONLY" * 8
        autostart._write(directory / "credential.cred", cipher)
        autostart._json(directory / "installation.json", {"schema": 1, "node": False, "credential_sha256": hashlib.sha256(cipher).hexdigest()})
        (directory / "credential.cred").write_bytes(cipher + b"changed")
        self.command.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            autostart._credential(self.state)
        self.command.assert_not_called()

    def test_decrypt_failure_reports_no_plaintext_or_broker_output(self):
        import hashlib
        _, directory, _ = autostart._paths(self.state)
        cipher = b"SYNTHETIC-CIPHERTEXT-ONLY" * 8
        autostart._write(directory / "credential.cred", cipher)
        autostart._json(directory / "installation.json", {"schema": 1, "node": False, "credential_sha256": hashlib.sha256(cipher).hexdigest()})
        self.command.side_effect = RuntimeError("Autostart command failed: systemd-creds. No command output was exported.")
        with self.assertRaisesRegex(RuntimeError, "No command output was exported"):
            autostart._credential(self.state)


    def test_disconnect_pauses_worker_without_stopping_local_services_until_unit_stop(self):
        settings = config.load_config(self.state)
        settings["headless"] = "auto"
        config.save_config(self.state, settings)
        states = []
        checks = 0
        def wait(delay):
            nonlocal checks
            checks += 1
            runtime_stop.assert_not_called()
            self.assertIn("disconnected", states)
            autostart._STOP = True
        with mock.patch.object(runtime, "start", return_value={"running": True, "ready": True}), mock.patch.object(runtime, "status", return_value={"running": True}), mock.patch.object(runtime, "stop") as runtime_stop, mock.patch.object(connection_background, "start", return_value=1) as start, mock.patch.object(connection_background, "status", return_value={"alive": False, "verified": False, "state": "stopped"}), mock.patch.object(connection_background, "disconnect") as disconnect, mock.patch.object(autostart, "_credential", return_value=MARKER), mock.patch.object(autostart, "_worker_status", side_effect=lambda directory, name: states.append(name)), mock.patch.object(autostart.time, "sleep", side_effect=wait):
            self.assertEqual(0, autostart.worker(self.state))
            self.assertEqual(1, checks)
            self.assertEqual(1, start.call_args.kwargs["wait_timeout"])
            runtime_stop.assert_called_once()
            disconnect.assert_called_once()

    def test_failed_tunnel_requests_service_restart_and_cleans_up(self):
        settings = config.load_config(self.state)
        settings["headless"] = "auto"
        config.save_config(self.state, settings)
        with mock.patch.object(runtime, "start", return_value={"running": True, "ready": True}), mock.patch.object(runtime, "stop") as runtime_stop, mock.patch.object(connection_background, "start", return_value=1), mock.patch.object(connection_background, "status", return_value={"alive": False, "state": "error"}), mock.patch.object(connection_background, "disconnect") as disconnect, mock.patch.object(autostart, "_credential", return_value=MARKER):
            self.assertEqual(1, autostart.worker(self.state))
            runtime_stop.assert_called_once()
            disconnect.assert_called_once()

    def test_physical_desktop_wait_requires_no_new_key_prompt(self):
        from framebridge_expanded.display_context import DisplayContextError
        states = []
        def wait(delay):
            autostart._STOP = True
        with mock.patch.object(runtime, "start", side_effect=DisplayContextError("PHYSICAL_X11_UNAVAILABLE_NO_VIRTUAL_FALLBACK")), mock.patch.object(runtime, "stop"), mock.patch.object(autostart, "_credential") as credential, mock.patch.object(autostart, "_worker_status", side_effect=lambda directory, name: states.append(name)), mock.patch.object(autostart.time, "sleep", side_effect=wait):
            self.assertEqual(0, autostart.worker(self.state))
            self.assertIn("waiting_for_graphical_login", states)
            credential.assert_not_called()
            self.key_prompt.assert_not_called()

    def test_enable_interrupt_rolls_back_unit_blob_and_exact_profile(self):
        before = (self.state / "config.json").read_bytes()
        def interrupted(*args, **kwargs):
            if args[:2] == ("enable", "--now"):
                raise KeyboardInterrupt()
            return self.fake_systemctl(*args, **kwargs)
        with mock.patch.object(autostart, "_systemctl", side_effect=interrupted):
            result = autostart.enable(self.state)
        self.assertEqual({"configured": False, "state": "cancelled"}, result)
        _, directory, unit = autostart._paths(self.state)
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.assertFalse(unit.exists())
        self.assertFalse((directory / "credential.cred").exists())
        self.assertFalse((directory / "installation.json").exists())

    def test_disable_privilege_cancellation_preserves_complete_installation(self):
        autostart.enable(self.state)
        _, directory, unit = autostart._paths(self.state)
        unit_before = unit.read_bytes()
        cipher_before = (directory / "credential.cred").read_bytes()
        def cancelled(args, **kwargs):
            if args == ["sudo", "-v"]:
                raise KeyboardInterrupt()
            return self.fake_command(args, **kwargs)
        self.command.side_effect = cancelled
        with self.assertRaises(KeyboardInterrupt):
            autostart.disable(self.state)
        self.assertEqual(unit_before, unit.read_bytes())
        self.assertEqual(cipher_before, (directory / "credential.cred").read_bytes())
        self.assertTrue((directory / "installation.json").exists())

    def test_unconfigured_manual_stop_does_not_create_autostart_directory(self):
        other = self.home / "other-profile"
        config.save_config(other, config.default_config())
        self.assertFalse(autostart.mark_manual_stop(other))
        self.assertFalse((other / "autostart").exists())

    def test_runtime_crash_requests_service_restart(self):
        with mock.patch.object(runtime, "start", return_value={"running": True, "ready": True}), mock.patch.object(runtime, "status", return_value={"running": False}), mock.patch.object(runtime, "stop") as runtime_stop, mock.patch.object(connection_background, "start", return_value=0), mock.patch.object(connection_background, "status", return_value={"alive": True, "verified": True, "state": "connected"}), mock.patch.object(connection_background, "disconnect") as disconnect, mock.patch.object(autostart, "_credential", return_value=MARKER):
            self.assertEqual(1, autostart.worker(self.state))
            runtime_stop.assert_called_once()
            disconnect.assert_called_once()

    def test_explicit_stop_intent_prevents_automatic_restart(self):
        _, directory, _ = autostart._paths(self.state)
        autostart._json(directory / "installation.json", {"schema": 1, "node": False})
        def explicitly_stopped(state):
            self.assertTrue(autostart.mark_manual_stop(state))
            return {"running": False}
        with mock.patch.object(runtime, "start", return_value={"running": True, "ready": True}), mock.patch.object(runtime, "status", side_effect=explicitly_stopped), mock.patch.object(runtime, "stop") as runtime_stop, mock.patch.object(connection_background, "start", return_value=0), mock.patch.object(connection_background, "status", return_value={"alive": True, "verified": True, "state": "connected"}), mock.patch.object(connection_background, "disconnect"), mock.patch.object(autostart, "_credential", return_value=MARKER):
            self.assertEqual(0, autostart.worker(self.state))
            runtime_stop.assert_called_once()
            self.assertTrue(autostart.manual_stop_requested(self.state))

    def test_stale_manual_stop_is_cleared_when_a_new_worker_starts(self):
        _, directory, _ = autostart._paths(self.state)
        autostart._json(directory / "installation.json", {"schema": 1, "node": False})
        self.assertTrue(autostart.mark_manual_stop(self.state))
        self.assertTrue(autostart.manual_stop_requested(self.state))
        with mock.patch.object(runtime, "start", return_value={"running": True, "ready": True}) as start, mock.patch.object(runtime, "status", return_value={"running": False}), mock.patch.object(runtime, "stop"), mock.patch.object(connection_background, "start", return_value=0), mock.patch.object(connection_background, "status", return_value={"alive": True, "verified": True, "state": "connected"}), mock.patch.object(connection_background, "disconnect"), mock.patch.object(autostart, "_credential", return_value=MARKER):
            self.assertEqual(1, autostart.worker(self.state))
            start.assert_called_once()
            self.assertFalse(autostart.manual_stop_requested(self.state))

    def test_stop_for_current_boot_cancels_restart_job_but_preserves_unit_and_credential(self):
        autostart.enable(self.state)
        _, directory, unit = autostart._paths(self.state)
        cipher = (directory / "credential.cred").read_bytes()
        original_unit = unit.read_bytes()
        self.system_calls.clear()
        result = autostart.stop_for_current_boot(self.state)
        self.assertEqual("stopped_for_current_boot", result["state"])
        self.assertEqual([("stop", unit.name)], self.system_calls)
        self.assertEqual(original_unit, unit.read_bytes())
        self.assertEqual(cipher, (directory / "credential.cred").read_bytes())
        self.assertTrue((directory / "installation.json").exists())

    def test_stop_for_current_boot_refuses_changed_unit(self):
        autostart.enable(self.state)
        _, _, unit = autostart._paths(self.state)
        unit.write_text(unit.read_text() + "# foreign change\n")
        self.system_calls.clear()
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            autostart.stop_for_current_boot(self.state)
        self.assertEqual([], self.system_calls)
        self.assertTrue(unit.exists())

    def test_stop_for_current_boot_unconfigured_does_not_create_directories(self):
        other = self.home / "unmanaged"
        config.save_config(other, config.default_config())
        self.assertFalse(autostart.stop_for_current_boot(other)["configured"])
        self.assertFalse((other / "autostart").exists())
        self.assertEqual([], self.system_calls)

    def test_cli_stop_cancels_service_restart_before_stopping_runtime(self):
        from framebridge_expanded import cli
        events = []
        def stop_unit(state):
            events.append("unit-stop")
        def stop_runtime(state):
            self.assertEqual(["unit-stop"], events)
            events.append("runtime-stop")
            return {"running": False}
        with mock.patch.object(autostart, "stop_for_current_boot", side_effect=stop_unit), mock.patch.object(runtime, "stop", side_effect=stop_runtime), mock.patch.object(cli, "_show"):
            self.assertEqual(0, cli.main(["--state-dir", str(self.state), "stop"]))
        self.assertEqual(["unit-stop", "runtime-stop"], events)
