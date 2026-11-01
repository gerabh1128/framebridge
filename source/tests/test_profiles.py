"""Private reference, deterministic routing and upgrade migration regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded import cli, config, profiles, package_upgrade


class ProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="profile-reference-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        patched = mock.patch.object(profiles, "_home", return_value=self.home)
        patched.start(); self.addCleanup(patched.stop)
        patched = mock.patch.dict(os.environ, {}, clear=True)
        patched.start(); self.addCleanup(patched.stop)
        patched = mock.patch.object(config, "DEFAULT_STATE", self.home / "legacy-default")
        patched.start(); self.addCleanup(patched.stop)
        self.state = self.home / "native-profile"
        self.settings = config.default_config("node", "TESTNODE")
        config.save_config(self.state, self.settings)

    @property
    def reference(self):
        return self.home / ".config/framebridge-profiles/default.json"

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def test_reference_contains_only_path_and_identity_not_configuration(self):
        result = profiles.select(self.state)
        value = json.loads(self.reference.read_text())
        self.assertEqual(set(value), {"schema_version", "state_dir", "node_id"})
        self.assertEqual(value["state_dir"], str(self.state))
        self.assertEqual(value["node_id"], self.settings["node_id"])
        self.assertEqual(stat.S_IMODE(self.reference.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.reference.parent.stat().st_mode), 0o700)
        self.assertTrue(result["selected"])

    def test_explicit_and_environment_destinations_override_preference(self):
        profiles.select(self.state)
        self.assertEqual(config.state_path(), self.state)
        self.assertEqual(cli.resolve_state(), self.state)
        with mock.patch.dict(os.environ, {"FRAMEBRIDGE_EXPANDED_STATE": "/chosen/canonical", "FRAMEBRIDGE_STATE_DIR": "/chosen/alias"}):
            self.assertEqual(config.state_path(), Path("/chosen/canonical"))
            self.assertEqual(cli.resolve_state("/chosen/explicit"), Path("/chosen/explicit"))
        with mock.patch.dict(os.environ, {"FRAMEBRIDGE_STATE_DIR": "/chosen/alias"}):
            self.assertEqual(config.state_path(), Path("/chosen/alias"))

    def test_absence_uses_legacy_default_without_scanning_or_writing(self):
        other = self.home / "another-native-profile"
        config.save_config(other, config.default_config())
        self.assertEqual(config.state_path(), config.DEFAULT_STATE)
        self.assertFalse(self.reference.exists())
        self.assertFalse((self.home / ".config").exists())

    def test_isolated_home_and_xdg_do_not_redirect_account_preference(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home / "isolated"), "XDG_CONFIG_HOME": str(self.home / "other")}):
            profiles.select(self.state)
        self.assertTrue(self.reference.is_file())
        self.assertFalse((self.home / "isolated").exists())
        self.assertFalse((self.home / "other").exists())

    def test_shared_legacy_framebridge_directory_is_not_changed_or_used(self):
        legacy = self.home / ".config/framebridge"
        legacy.mkdir(parents=True, mode=0o775); legacy.chmod(0o775)
        (legacy / "prompt-indicator.bash").write_text("legacy")
        profiles.select(self.state)
        self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o775)
        self.assertEqual((legacy / "prompt-indicator.bash").read_text(), "legacy")

    def test_missing_selected_profile_is_not_replaced_with_empty_default(self):
        profiles.select(self.state)
        (self.state / "config.json").unlink()
        with self.assertRaises(profiles.ProfileSelectionError):
            config.state_path()
        code, text, _ = self.invoke(["--json", "health"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(text)["result"]["error"], "PROFILE_UNAVAILABLE")
        self.assertFalse(config.DEFAULT_STATE.exists())

    def test_selected_profile_identity_change_is_refused(self):
        profiles.select(self.state)
        config.save_config(self.state, config.default_config("node", "REPLACED"))
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "identity changed"):
            profiles.selected()

    def test_reference_symlink_does_not_read_or_change_target(self):
        profiles.select(self.state)
        target = self.home / "target"
        original = self.reference.read_bytes()
        self.reference.rename(target); self.reference.symlink_to(target)
        with self.assertRaises(OSError): profiles.selected()
        with self.assertRaises(OSError): profiles.select(self.state)
        self.assertEqual(target.read_bytes(), original)

    def test_hardlinked_or_shared_reference_is_refused(self):
        profiles.select(self.state)
        alias = self.home / "alias"
        os.link(self.reference, alias)
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "unsafe"):
            profiles.selected()
        alias.unlink(); self.reference.chmod(0o640)
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "unsafe"):
            profiles.selected()
        self.assertEqual(stat.S_IMODE(self.reference.stat().st_mode), 0o640)

    def test_shared_preference_directory_is_refused_without_repair(self):
        profiles.select(self.state); self.reference.parent.chmod(0o750)
        with self.assertRaises(profiles.ProfileSelectionError): profiles.selected()
        self.assertEqual(stat.S_IMODE(self.reference.parent.stat().st_mode), 0o750)

    def test_profile_directory_symlink_is_refused(self):
        alias = self.home / "alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "symbolic link"):
            profiles.select(alias)

    def test_profile_parent_symlink_is_refused(self):
        alias = self.home / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "symbolic link"):
            profiles.select(alias / self.state.name)

    def test_malformed_reference_is_not_silently_discarded(self):
        profiles.select(self.state); self.reference.write_text("not JSON")
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "invalid"):
            profiles.selected()

    def test_migration_uses_only_given_verified_capture_and_preserves_reference(self):
        result = profiles.migrate(self.state)
        self.assertTrue(result["selected"])
        other = self.home / "other"
        config.save_config(other, config.default_config("node", "OTHER"))
        before = self.reference.read_bytes()
        result = profiles.migrate(other)
        self.assertTrue(result["existing_reference_preserved"])
        self.assertEqual(self.reference.read_bytes(), before)
        self.assertEqual(profiles.selected(), self.state)

    def test_ambiguous_upgrade_creates_diagnostic_not_an_arbitrary_selection(self):
        result = profiles.migrate(self.state, ambiguous=True)
        self.assertTrue(result["ambiguous"]); self.assertFalse(result["selected"])
        self.assertEqual(json.loads(self.reference.read_text()), {"schema_version": 1, "ambiguous": True})
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "explicit choice"):
            cli.resolve_state()
        self.assertEqual(self.invoke(["--json", "health"])[0], 2)
        self.assertEqual(self.invoke(["--state-dir", str(self.state), "profile", "select"])[0], 0)
        self.assertEqual(cli.resolve_state(), self.state)

    def test_show_and_clear_do_not_require_a_working_profile(self):
        profiles.select(self.state); (self.state / "config.json").unlink()
        self.assertEqual(self.invoke(["profile", "show"])[0], 2)
        code, text, _ = self.invoke(["profile", "clear", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(text)["result"]["profiles_and_credentials_retained"])
        self.assertTrue(self.state.is_dir())
        self.assertFalse(self.reference.exists())

    def test_help_and_menu_exit_stay_available_for_ambiguous_reference(self):
        profiles.migrate(self.state, ambiguous=True)
        with mock.patch.object(cli, "_module") as modules, mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            self.assertEqual(self.invoke(["help", "--json"])[0], 0)
        modules.assert_called_once_with("guide")
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="0"):
            self.assertEqual(self.invoke([])[0], 0)

    def test_status_selects_preferred_profile_without_mutating_reference(self):
        profiles.select(self.state); before = self.reference.read_bytes()
        lifecycle = SimpleNamespace(status=mock.Mock(return_value={"ok": True}))
        with mock.patch.object(cli, "_module", return_value=lifecycle):
            self.assertEqual(self.invoke(["status", "--json"])[0], 0)
        lifecycle.status.assert_called_once_with(self.state)
        self.assertEqual(self.reference.read_bytes(), before)

    def test_connect_success_records_profile_but_failure_does_not(self):
        for code in (1, 0):
            with mock.patch.object(cli, "connect_chatgpt", return_value=code):
                self.assertEqual(self.invoke(["--state-dir", str(self.state), "connect"])[0], code)
            self.assertEqual(self.reference.exists(), code == 0)

    def test_successful_boot_enable_records_profile_and_disable_does_not_retarget(self):
        lifecycle = SimpleNamespace(boot_enable=mock.Mock(return_value={"ok": True}),
                                    boot_disable=mock.Mock(return_value={"ok": True}))
        with mock.patch.object(cli, "_module", return_value=lifecycle):
            self.assertEqual(self.invoke(["--state-dir", str(self.state), "service", "boot", "enable"])[0], 0)
            before = self.reference.read_bytes()
            self.assertEqual(self.invoke(["--state-dir", str(self.state), "service", "boot", "disable"])[0], 0)
        self.assertEqual(self.reference.read_bytes(), before)

    def test_upgrade_owner_forwards_ambiguity_only_to_reference_migration(self):
        row = {"uid": 1000, "user": "owner", "home": "/home/owner",
               "state": str(self.state), "config_sha256": "a" * 64,
               "pid": 1234, "birth": "99", "default_profile_ambiguous": True}
        account = SimpleNamespace(pw_name="owner", pw_dir="/home/owner")
        with mock.patch.object(package_upgrade.pwd, "getpwuid", return_value=account), \
             mock.patch.object(package_upgrade.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"ok":true}')) as run:
            package_upgrade._owner(row, "user-profile")
            self.assertIn("--ambiguous-profiles", run.call_args.args[0])
            package_upgrade._owner(row, "user-ready")
            self.assertNotIn("--ambiguous-profiles", run.call_args.args[0])

    def test_concurrent_explicit_choice_is_not_replaced_by_upgrade_absence_check(self):
        other = self.home / "explicit-choice"
        config.save_config(other, config.default_config("node", "EXPLICIT"))
        original_read = profiles._read
        first = True
        def competing_selection():
            nonlocal first
            if first:
                first = False
                profiles.select(other)
                return None
            return original_read()
        with mock.patch.object(profiles, "_read", side_effect=competing_selection):
            result = profiles.migrate(self.state)
        self.assertTrue(result["existing_reference_preserved"])
        self.assertEqual(profiles.selected(), other)

    def test_unsafe_selection_lock_is_not_replaced_or_repaired(self):
        profiles.select(self.state)
        lock = self.reference.parent / ".selection.lock"
        lock.chmod(0o640)
        before = self.reference.read_bytes()
        with self.assertRaisesRegex(profiles.ProfileSelectionError, "lock is unsafe"):
            profiles.select(self.state)
        self.assertEqual(self.reference.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o640)

    def test_null_reference_identity_cannot_disable_binding_validation(self):
        profiles.select(self.state)
        value = json.loads(self.reference.read_text()); value['node_id'] = None
        self.reference.write_text(json.dumps(value))
        config.save_config(self.state, config.default_config('node', 'REPLACED'))
        with self.assertRaisesRegex(profiles.ProfileSelectionError, 'canonical UUID'):
            profiles.selected()
        # Internal callers must not be able to use None as "no expectation".
        with self.assertRaisesRegex(profiles.ProfileSelectionError, 'identity changed'):
            profiles._profile(self.state, None)

    def test_reference_identity_requires_a_canonical_uuid_string(self):
        profiles.select(self.state)
        value = json.loads(self.reference.read_text())
        invalid = (None, False, 123, [], {}, '', 'invalid',
                   'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA',
                   '{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}',
                   'aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa')
        for identity in invalid:
            with self.subTest(identity=identity):
                value['node_id'] = identity
                self.reference.write_text(json.dumps(value))
                with self.assertRaisesRegex(profiles.ProfileSelectionError, 'canonical UUID'):
                    profiles.selected()

    def test_explicit_select_recovers_only_invalid_content_with_safe_metadata(self):
        malformed = (b'not JSON', b'{', b'\xff', b'[]', b'{}',
                     b'{"schema_version":true}', b'{"schema_version":2}',
                     b'{"schema_version":1,"ambiguous":1}',
                     b'x' * (profiles.MAXIMUM + 1))
        config_before = (self.state / 'config.json').read_bytes()
        for raw in malformed:
            with self.subTest(raw=raw[:50]):
                profiles.select(self.state)
                self.reference.write_bytes(raw)
                with self.assertRaises(profiles.ProfileSelectionError):
                    profiles.selected()
                self.assertEqual(self.reference.read_bytes(), raw)
                code, _, _ = self.invoke(['--state-dir', str(self.state), 'profile', 'select'])
                self.assertEqual(code, 0)
                self.assertEqual(profiles.selected(), self.state)
                self.assertEqual((self.state / 'config.json').read_bytes(), config_before)

    def test_explicit_clear_recovers_invalid_json_and_schema_without_changing_profile(self):
        config_before = (self.state / 'config.json').read_bytes()
        for raw in (b'bad JSON', b'{"schema_version":9}',
                    b'{"schema_version":1,"state_dir":"/safe","node_id":null}'):
            with self.subTest(raw=raw):
                profiles.select(self.state)
                self.reference.write_bytes(raw)
                code, text, _ = self.invoke(['profile', 'clear', '--json'])
                self.assertEqual(code, 0)
                self.assertTrue(json.loads(text)['result']['reference_removed'])
                self.assertFalse(self.reference.exists())
                self.assertEqual((self.state / 'config.json').read_bytes(), config_before)

    def test_upgrade_never_repairs_corrupt_preference_implicitly(self):
        profiles.select(self.state); self.reference.write_bytes(b'not JSON')
        for ambiguous in (False, True):
            with self.assertRaises(profiles.ProfileSelectionError):
                profiles.migrate(self.state, ambiguous=ambiguous)
            self.assertEqual(self.reference.read_bytes(), b'not JSON')

    def test_explicit_recovery_does_not_bypass_insecure_reference_metadata(self):
        profiles.select(self.state); self.reference.write_bytes(b'not JSON')
        self.reference.chmod(0o640)
        for action in (lambda: profiles.select(self.state), profiles.clear):
            with self.assertRaisesRegex(profiles.ProfileSelectionError, 'unsafe'):
                action()
            self.assertEqual(self.reference.read_bytes(), b'not JSON')
            self.assertEqual(stat.S_IMODE(self.reference.stat().st_mode), 0o640)
        self.reference.chmod(0o600)
        alias = self.home / 'same-reference'; os.link(self.reference, alias)
        for action in (lambda: profiles.select(self.state), profiles.clear):
            with self.assertRaisesRegex(profiles.ProfileSelectionError, 'unsafe'):
                action()
            self.assertEqual(alias.read_bytes(), b'not JSON')

    def test_explicit_clear_refuses_symlink_and_unsafe_writer_lock(self):
        profiles.select(self.state)
        target = self.home / 'reference-target'
        self.reference.rename(target); self.reference.symlink_to(target)
        before = target.read_bytes()
        with self.assertRaises(OSError): profiles.clear()
        self.assertEqual(target.read_bytes(), before)
        self.reference.unlink(); target.rename(self.reference)
        lock = self.reference.parent / '.selection.lock'; lock.chmod(0o640)
        with self.assertRaisesRegex(profiles.ProfileSelectionError, 'lock is unsafe'):
            profiles.clear()
        self.assertEqual(self.reference.read_bytes(), before)

    def test_clear_waits_for_shared_writer_lock_before_reading_or_removing(self):
        profiles.select(self.state)
        lock = self.reference.parent / '.selection.lock'
        held = os.open(lock, os.O_RDWR)
        real_flock = profiles.fcntl.flock
        real_flock(held, profiles.fcntl.LOCK_EX)
        waiting, finished = threading.Event(), threading.Event()
        errors, outcomes = [], []
        def observe_lock(fd, operation):
            if fd != held and operation == profiles.fcntl.LOCK_EX:
                waiting.set()
            return real_flock(fd, operation)
        def clear_in_thread():
            try: outcomes.append(profiles.clear())
            except BaseException as error: errors.append(error)
            finally: finished.set()
        thread = threading.Thread(target=clear_in_thread)
        try:
            with mock.patch.object(profiles.fcntl, 'flock', side_effect=observe_lock), \
                 mock.patch.object(profiles, '_read', wraps=profiles._read) as read:
                thread.start()
                self.assertTrue(waiting.wait(2))
                self.assertFalse(finished.wait(0.03))
                read.assert_not_called()
                self.assertTrue(self.reference.exists())
                real_flock(held, profiles.fcntl.LOCK_UN)
                thread.join(2)
        finally:
            real_flock(held, profiles.fcntl.LOCK_UN); os.close(held)
            if thread.is_alive(): thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertFalse(errors)
        self.assertTrue(outcomes[0]['reference_removed'])
        self.assertFalse(self.reference.exists())


if __name__ == "__main__":
    unittest.main()
