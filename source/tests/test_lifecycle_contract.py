import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import autostart, config, lifecycle, runtime, connection_background


class LifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.home / "profile"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Path, "home", return_value=self.home).start()
        self.settings = config.default_config()
        self.settings["tunnel_id"] = "tunnel_0123456789abcdef0123456789abcdef"
        config.save_config(self.state, self.settings)
        _, self.directory, self.unit = autostart._paths(self.state)
        self.unit.parent.mkdir(mode=0o700, parents=True)
        unit = autostart.unit_text(self.state, self.directory / "credential.cred").encode()
        autostart._write(self.unit, unit)
        cipher = b"SYNTHETIC-ENCRYPTED-FIXTURE-NOT-A-CREDENTIAL"
        autostart._write(self.directory / "credential.cred", cipher)
        self.record = {"schema": 1, "unit": self.unit.name,
                       "unit_sha256": hashlib.sha256(unit).hexdigest(),
                       "credential_sha256": hashlib.sha256(cipher).hexdigest(),
                       "connection_settings_sha256": autostart._connection_public_digest(self.settings),
                       "node": False, "headless_changed": False}
        autostart._json(self.directory / "installation.json", self.record)
        self.enabled = True
        self.active = True
        self.calls = []
        self.system = mock.patch.object(autostart, "_systemctl", side_effect=self.systemctl).start()
        self.command = mock.patch.object(autostart, "_command", side_effect=AssertionError("No OS authentication or encryption command belongs in these unit fixtures.")).start()
        mock.patch.object(autostart, "_linger", return_value=True).start()
        self.runtime_start = mock.patch.object(runtime, "start", return_value={"running": True, "ready": True}).start()
        self.runtime_status = mock.patch.object(runtime, "status", return_value={"running": True, "ready": True}).start()
        self.runtime_stop = mock.patch.object(runtime, "stop", return_value={"ok": True, "running": False}).start()
        self.background_status = mock.patch.object(connection_background, "status", return_value={"ok": True, "alive": False, "verified": False, "state": "stopped"}).start()
        self.background_stop = mock.patch.object(connection_background, "disconnect", return_value={"ok": True, "alive": False, "state": "stopped"}).start()

    def systemctl(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "enable":
            self.enabled = True
        if args[0] == "disable":
            self.enabled = False
        if args[0] == "start" or "--now" in args and args[0] == "enable":
            self.active = True
        if args[0] == "stop" or "--now" in args and args[0] == "disable":
            self.active = False
        body = ("ActiveState=" + ("active" if self.active else "inactive")
                + "\nSubState=" + ("running" if self.active else "dead")
                + "\nUnitFileState=" + ("enabled" if self.enabled else "disabled") + "\n").encode()
        return subprocess.CompletedProcess(args, 0, body, b"")

    def test_boot_disable_preserves_execution_cipher_and_profile(self):
        paths = [self.state / "config.json", self.directory / "credential.cred", self.directory / "installation.json", self.unit]
        before = {p: p.read_bytes() for p in paths}
        result = lifecycle.boot_disable(self.state)
        self.assertEqual("boot_disabled", result["state"])
        self.assertTrue(self.active)
        self.assertFalse(self.enabled)
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        self.assertIn(("disable", self.unit.name), self.calls)
        self.runtime_stop.assert_not_called()
        self.background_stop.assert_not_called()

    def test_boot_enable_does_not_start_an_inactive_stack(self):
        self.active = self.enabled = False
        result = lifecycle.boot_enable(self.state)
        self.assertTrue(result["current_execution_unchanged"])
        self.assertTrue(self.enabled)
        self.assertFalse(self.active)
        self.assertIn(("enable", self.unit.name), self.calls)
        self.runtime_start.assert_not_called()

    def test_service_stop_preserves_boot_and_material(self):
        before = (self.directory / "credential.cred").read_bytes()
        result = lifecycle.service_stop(self.state)
        self.assertTrue(result["ok"])
        self.assertTrue(self.enabled)
        self.assertFalse(self.active)
        self.assertEqual(before, (self.directory / "credential.cred").read_bytes())
        self.assertTrue(autostart.manual_stop_requested(self.state))
        self.assertTrue(autostart.connection_requested(self.state))
        self.assertNotIn(("disable", self.unit.name), self.calls)

    def test_service_start_retains_disabled_boot_policy(self):
        self.active = self.enabled = False
        autostart.mark_manual_stop(self.state)
        result = lifecycle.service_start(self.state)
        self.assertEqual("start_requested", result["state"])
        self.assertFalse(self.enabled)
        self.assertTrue(self.active)
        self.assertFalse(autostart.manual_stop_requested(self.state))
        self.assertNotIn(("enable", self.unit.name), self.calls)

    def test_disconnect_persists_intent_without_stopping_local_tools(self):
        result = lifecycle.disconnect(self.state)
        self.assertTrue(result["ok"])
        self.assertFalse(autostart.connection_requested(self.state))
        self.assertEqual("until_explicit_connect", result["scope"])
        self.runtime_stop.assert_not_called()
        self.assertFalse(any(x[0] in {"stop", "disable"} for x in self.calls))
        self.assertEqual("disconnected", lifecycle.status(self.state)["desired_connection"])

    def test_connection_intent_is_not_tied_to_a_boot_identifier(self):
        lifecycle.disconnect(self.state)
        record = json.loads((self.directory / "connection-intent.json").read_text())
        self.assertNotIn("boot_id", record)
        self.assertFalse(autostart.connection_requested(self.state))
        revision = autostart.connection_request_revision(self.state)
        autostart._set_connection_requested(self.state, True)
        self.assertTrue(autostart.connection_requested(self.state))
        self.assertGreater(autostart.connection_request_revision(self.state), revision)

    def test_saved_resume_wakes_transport_without_stopping_local_tools(self):
        lifecycle.disconnect(self.state)
        self.background_stop.reset_mock()
        self.calls.clear()
        current = {"configured": True, "enabled": True, "service_active": True,
                   "runtime": {"running": True, "ready": True},
                   "connection": {"alive": False, "verified": False},
                   "worker": {"state": "disconnected"}}
        with mock.patch.object(autostart, "status", return_value=current):
            autostart.resume_connection(self.state)
        self.assertTrue(autostart.connection_requested(self.state))
        self.assertFalse(any(x[0] in {"stop", "start"} for x in self.calls))
        self.background_stop.assert_not_called()
        self.runtime_stop.assert_not_called()
        self.command.assert_not_called()

    def test_existing_legacy_enable_repairs_a_disabled_unit(self):
        self.enabled = self.active = False
        result = autostart.enable(self.state)
        self.assertTrue(self.enabled)
        self.assertTrue(self.active)
        self.assertIn(("enable", "--now", self.unit.name), self.calls)

    def test_changed_unit_is_rejected_before_boot_or_stop_actions(self):
        self.unit.write_text("changed")
        self.unit.chmod(0o600)
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            lifecycle.boot_disable(self.state)
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            lifecycle.service_stop(self.state)
        self.assertEqual([], self.calls)

    def test_forget_removes_managed_connection_but_retains_profile_and_linger(self):
        before = (self.state / "config.json").read_bytes()
        autostart._set_connection_requested(self.state, False)
        result = lifecycle.forget_credentials(self.state)
        self.assertTrue(result["profile_preserved"])
        self.assertTrue(result["linger_preserved"])
        self.assertEqual(before, (self.state / "config.json").read_bytes())
        self.assertFalse(self.unit.exists())
        self.assertFalse((self.directory / "credential.cred").exists())
        self.assertFalse((self.directory / "installation.json").exists())
        self.assertFalse((self.directory / "connection-intent.json").exists())
        self.assertEqual("not_configured", lifecycle.status(self.state)["desired_connection"])
        self.command.assert_not_called()

    def test_forget_refuses_changed_cipher_without_removing_anything(self):
        p = self.directory / "credential.cred"
        p.write_bytes(b"changed")
        p.chmod(0o600)
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            lifecycle.forget_credentials(self.state)
        self.assertTrue(self.unit.exists())
        self.assertTrue(p.exists())
        self.assertTrue((self.directory / "installation.json").exists())
        self.assertEqual([], self.calls)

    def test_paused_worker_never_decrypts_or_starts_a_transport(self):
        lifecycle.disconnect(self.state)
        with mock.patch.object(autostart, "_credential") as credential, \
             mock.patch.object(connection_background, "start") as launch, \
             mock.patch.object(autostart.time, "sleep", side_effect=lambda _: setattr(autostart, "_STOP", True)):
            self.assertEqual(0, autostart.worker(self.state))
        credential.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(autostart.connection_requested(self.state))

    def test_explicit_connect_resumes_paused_worker_without_runtime_restart(self):
        lifecycle.disconnect(self.state)
        sleeps = 0
        def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                autostart._set_connection_requested(self.state, True)
            else:
                autostart._STOP = True
        with mock.patch.object(autostart, "_credential", return_value="SYNTHETIC-NOT-A-REAL-KEY") as credential, \
             mock.patch.object(connection_background, "start") as launch, \
             mock.patch.object(autostart.time, "sleep", side_effect=sleep):
            self.assertEqual(0, autostart.worker(self.state))
        self.assertEqual(1, launch.call_count)
        self.assertEqual(1, credential.call_count)
        self.assertEqual(1, self.runtime_start.call_count)
        self.assertEqual(1, self.runtime_stop.call_count)  # Final simulated unit stop only.

    def test_legacy_current_only_disconnect_is_not_restarted_in_same_worker(self):
        sleeps = 0
        def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                autostart._STOP = True
        states = [{"alive": False, "state": "stopped"},
                  {"alive": True, "verified": True, "state": "ready"},
                  {"alive": False, "state": "stopped"}]
        self.background_status.side_effect = states
        with mock.patch.object(autostart, "_credential", return_value="SYNTHETIC-NOT-A-REAL-KEY"), \
             mock.patch.object(connection_background, "start") as launch, \
             mock.patch.object(autostart.time, "sleep", side_effect=sleep):
            self.assertEqual(0, autostart.worker(self.state))
        self.assertEqual(1, launch.call_count)
        self.assertEqual(1, self.runtime_start.call_count)

    def test_transport_error_retries_without_restarting_local_runtime(self):
        self.background_status.return_value = {"alive": False, "state": "error"}
        sleeps = 0
        clock = [0.0]
        def sleep(_):
            nonlocal sleeps
            sleeps += 1
            clock[0] += 15
            if sleeps == 2:
                autostart._STOP = True
        with mock.patch.object(autostart, "_credential", return_value="SYNTHETIC-NOT-A-REAL-KEY"), \
             mock.patch.object(connection_background, "start") as launch, \
             mock.patch.object(autostart.time, "sleep", side_effect=sleep), \
             mock.patch.object(autostart.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(0, autostart.worker(self.state))
        self.assertEqual(2, launch.call_count)
        self.assertEqual(1, self.runtime_start.call_count)


    def test_connect_respects_an_explicit_disabled_boot_policy(self):
        lifecycle.boot_disable(self.state)
        self.calls.clear()
        current = {"configured": True, "enabled": False, "service_active": True,
                   "runtime": {"running": True, "ready": True},
                   "connection": {"alive": False, "verified": False},
                   "worker": {"state": "disconnected"}}
        with mock.patch.object(autostart, "status", return_value=current):
            autostart.resume_connection(self.state)
        self.assertFalse(autostart.boot_requested(self.state))
        self.assertFalse(any(x[0] == "enable" for x in self.calls))

    def test_connect_starts_disabled_inactive_service_without_enabling_boot(self):
        lifecycle.boot_disable(self.state)
        self.active = False
        self.calls.clear()
        current = {"configured": True, "enabled": False, "service_active": False,
                   "runtime": {"running": False, "ready": False}, "worker": {"state": "stopped"}}
        with mock.patch.object(autostart, "status", return_value=current):
            autostart.resume_connection(self.state)
        self.assertIn(("start", self.unit.name), self.calls)
        self.assertFalse(self.enabled)
        self.assertFalse(any(x[0] == "enable" for x in self.calls))

    def test_failed_boot_policy_action_restores_previous_intent(self):
        lifecycle.boot_enable(self.state)
        before = (self.directory / "boot-policy.json").read_bytes()
        with mock.patch.object(autostart, "_systemctl", side_effect=RuntimeError("synthetic service failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                lifecycle.boot_disable(self.state)
        self.assertEqual(before, (self.directory / "boot-policy.json").read_bytes())
        self.assertTrue(autostart.boot_requested(self.state))

    def _new_node(self):
        state = self.home / "new-node"
        settings = config.default_config(role="node")
        settings.update(headless="auto", broker_url="https://example.test:20878")
        config.save_config(state, settings)
        self.active = self.enabled = False
        def command(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, b"systemd 257\n" if args[:2] == ["systemd-creds", "--version"] else b"", b"")
        self.command.side_effect = command
        return state

    def test_initial_node_service_start_needs_no_chatgpt_key_or_extra_question(self):
        state = self._new_node()
        with mock.patch("builtins.input", side_effect=AssertionError("No redundant startup question")), \
             mock.patch.object(autostart.getpass, "getpass", side_effect=AssertionError("Node has no ChatGPT key")), \
             mock.patch.object(autostart, "_enabled_units", return_value=[]):
            result = lifecycle.service_start(state)
        _, directory, unit = autostart._paths(state)
        self.assertTrue(result["initial_configuration"])
        self.assertTrue(result["boot_policy_changed"])
        self.assertIn(("enable", "--now", unit.name), self.calls)
        self.assertFalse((directory / "credential.cred").exists())
        self.assertTrue(json.loads((directory / "installation.json").read_text())["node"])

    def test_initial_node_boot_enable_does_not_start_current_execution(self):
        state = self._new_node()
        with mock.patch("builtins.input", side_effect=AssertionError("No redundant startup question")), \
             mock.patch.object(autostart.getpass, "getpass", side_effect=AssertionError("Node has no ChatGPT key")), \
             mock.patch.object(autostart, "_enabled_units", return_value=[]):
            result = lifecycle.boot_enable(state)
        _, directory, unit = autostart._paths(state)
        self.assertTrue(result["initial_configuration"])
        self.assertTrue(result["current_execution_unchanged"])
        self.assertTrue(autostart.boot_requested(state))
        self.assertFalse(self.active)
        self.assertNotIn(("enable", "--now", unit.name), self.calls)
        self.assertFalse((directory / "credential.cred").exists())



    def test_status_handles_a_profile_with_an_empty_autostart_directory(self):
        state = self.home / "not-configured"
        config.save_config(state, config.default_config())
        (state / "autostart").mkdir(mode=0o700)
        result = lifecycle.status(state)
        self.assertTrue(result["ok"])
        self.assertFalse(result["boot"]["configured"])
        self.assertEqual("not_configured", result["desired_connection"])
        self.assertEqual("not_configured", autostart.status(state)["desired_connection"])

    def test_persistent_transport_error_respects_retry_delay(self):
        self.background_status.return_value = {"alive": False, "state": "error"}
        sleeps = 0
        def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                autostart._STOP = True
        with mock.patch.object(autostart, "_credential", return_value="SYNTHETIC-NOT-A-REAL-KEY"), \
             mock.patch.object(connection_background, "start") as launch, \
             mock.patch.object(autostart.time, "sleep", side_effect=sleep), \
             mock.patch.object(autostart.time, "monotonic", return_value=0.0):
            self.assertEqual(0, autostart.worker(self.state))
        self.assertEqual(1, launch.call_count)
        self.assertEqual(1, self.runtime_start.call_count)



    def test_service_stop_does_not_confuse_query_success_with_termination(self):
        results = [{"ok": True, "running": True, "processes": []},
                   {"ok": True, "running": False, "processes": [{"alive": True}]}]
        for observed in results:
            with self.subTest(observed=observed), \
                 mock.patch.object(runtime, "stop", return_value=observed), \
                 mock.patch.object(lifecycle.time, "monotonic", side_effect=[0.0, 3.0]):
                result = lifecycle.service_stop(self.state)
            self.assertFalse(result["ok"])
            self.assertEqual("stop_incomplete", result["state"])
            self.assertTrue((self.directory / "credential.cred").exists())

    def test_forget_retains_material_if_local_tools_remain_running(self):
        before = (self.directory / "credential.cred").read_bytes()
        with mock.patch.object(runtime, "stop", return_value={"ok": True, "running": True}), \
             mock.patch.object(lifecycle.time, "monotonic", side_effect=[0.0, 3.0]):
            with self.assertRaisesRegex(RuntimeError, "material was retained"):
                lifecycle.forget_credentials(self.state)
        self.assertEqual(before, (self.directory / "credential.cred").read_bytes())
        self.assertTrue(self.unit.exists())
        self.assertTrue((self.directory / "installation.json").exists())



if __name__ == "__main__":
    unittest.main()
