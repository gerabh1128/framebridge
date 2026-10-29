"""Opt-in startup tests use only owned temporary homes and harmless stubs."""
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "expanded"))
from framebridge_expanded import console_startup as startup


@unittest.skipIf(os.geteuid() == 0, "Unprivileged user profile tests")
class ConsoleStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fb-login-startup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.root / "state"

    def existing(self, name=".profile", data=b"# user's existing startup\n", mode=0o640):
        path = self.home / name
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def assert_ok(self, value):
        self.assertTrue(value.get("ok"), value)
        return value

    def manifest(self):
        return json.loads((self.state / "console-login/manifest.json").read_text())

    def test_existing_bytes_mode_and_exact_private_backup_restore(self):
        data = b"printf '%s' 'existing login content'"
        path = self.existing(".bash_profile", data)
        result = self.assert_ok(startup.enable(self.state, home=self.home))
        self.assertTrue(result["enabled"])
        self.assertEqual(result["startup_file"], str(path))
        self.assertTrue(path.read_bytes().startswith(data + b"\n"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        record = self.manifest()
        backup = self.state / "console-login" / record["backup_name"]
        self.assertEqual(backup.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(self.assert_ok(startup.status(self.state, home=self.home))["enabled"])
        result = self.assert_ok(startup.disable(self.state, home=self.home))
        self.assertFalse(result["enabled"])
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_effective_bash_startup_priority(self):
        self.existing(".profile", b"# lower priority\n")
        login = self.existing(".bash_login", b"# selected login\n")
        result = self.assert_ok(startup.enable(self.state, home=self.home))
        self.assertEqual(result["startup_file"], str(login))
        self.assertEqual((self.home / ".profile").read_bytes(), b"# lower priority\n")

    def test_new_profile_is_removed_on_disable(self):
        self.assertTrue(self.assert_ok(startup.enable(self.state, home=self.home))["changed"])
        path = self.home / ".profile"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assert_ok(startup.disable(self.state, home=self.home))
        self.assertFalse(path.exists())

    def test_enable_and_disable_are_idempotent(self):
        path = self.existing()
        self.assert_ok(startup.enable(self.state, home=self.home))
        installed = path.read_bytes()
        again = self.assert_ok(startup.enable(self.state, home=self.home))
        self.assertFalse(again["changed"])
        self.assertEqual(path.read_bytes(), installed)
        self.assertEqual(len(list((self.state / "console-login").glob("backup-*.bin"))), 1)
        self.assert_ok(startup.disable(self.state, home=self.home))
        self.assertFalse(self.assert_ok(startup.disable(self.state, home=self.home))["changed"])

    def test_reenable_preserves_previous_recovery_history(self):
        path = self.existing(data=b"# baseline one\n")
        self.assert_ok(startup.enable(self.state, home=self.home))
        self.assert_ok(startup.disable(self.state, home=self.home))
        path.write_bytes(b"# baseline two\n")
        self.assert_ok(startup.enable(self.state, home=self.home))
        self.assertEqual(len(list((self.state / "console-login").glob("backup-*.bin"))), 2)
        self.assertEqual(len(list((self.state / "console-login").glob("history-*.json"))), 1)
        self.assert_ok(startup.disable(self.state, home=self.home))
        self.assertEqual(path.read_bytes(), b"# baseline two\n")

    def test_later_edits_are_never_overwritten(self):
        path = self.existing()
        self.assert_ok(startup.enable(self.state, home=self.home))
        path.write_bytes(path.read_bytes() + b"# later user change\n")
        changed = path.read_bytes()
        for operation in (startup.disable, startup.enable, startup.status):
            reply = operation(self.state, home=self.home)
            self.assertFalse(reply["ok"])
            self.assertEqual(reply["error"], "CONSOLE_LOGIN_STARTUP_CHANGED")
        self.assertEqual(path.read_bytes(), changed)

    def test_symlink_and_nonregular_startup_are_refused(self):
        target = self.root / "outside"
        target.write_bytes(b"# unchanged outside\n")
        (self.home / ".bash_profile").symlink_to(target)
        reply = startup.enable(self.state, home=self.home)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "CONSOLE_LOGIN_SYMLINK_REFUSED")
        self.assertEqual(target.read_bytes(), b"# unchanged outside\n")
        (self.home / ".bash_profile").unlink()
        (self.home / ".bash_profile").mkdir()
        self.assertEqual(startup.enable(self.state, home=self.home)["error"],
                         "CONSOLE_LOGIN_FILE_NOT_REGULAR")

    def test_competing_profile_is_refused(self):
        path = self.existing()
        self.assert_ok(startup.enable(self.state, home=self.home))
        installed = path.read_bytes()
        other = self.root / "other-state"
        reply = startup.enable(other, home=self.home)
        self.assertEqual(reply["error"], "CONSOLE_LOGIN_OTHER_PROFILE_ENABLED")
        self.assertEqual(path.read_bytes(), installed)
        self.assertFalse((other / "console-login/manifest.json").exists())

    def test_foreign_untracked_block_is_refused(self):
        path = self.existing(data=b"# BEGIN FRAMEBRIDGE CONSOLE LOGIN v1 foreign\n")
        before = path.read_bytes()
        reply = startup.enable(self.state, home=self.home)
        self.assertEqual(reply["error"], "CONSOLE_LOGIN_OTHER_PROFILE_ENABLED")
        self.assertEqual(path.read_bytes(), before)

    def test_priority_change_is_reported_but_own_hook_can_be_removed(self):
        path = self.existing(data=b"# restored profile\n")
        self.assert_ok(startup.enable(self.state, home=self.home))
        higher = self.existing(".bash_profile", b"# newly preferred file\n")
        reply = startup.status(self.state, home=self.home)
        self.assertEqual(reply["error"], "CONSOLE_LOGIN_STARTUP_PRIORITY_CHANGED")
        self.assert_ok(startup.disable(self.state, home=self.home))
        self.assertEqual(path.read_bytes(), b"# restored profile\n")
        self.assertEqual(higher.read_bytes(), b"# newly preferred file\n")

    def test_modified_backup_refuses_restore(self):
        path = self.existing()
        self.assert_ok(startup.enable(self.state, home=self.home))
        installed = path.read_bytes()
        record = self.manifest()
        backup = self.state / "console-login" / record["backup_name"]
        backup.write_bytes(b"# tampered recovery\n")
        reply = startup.disable(self.state, home=self.home)
        self.assertEqual(reply["error"], "CONSOLE_LOGIN_BACKUP_MISMATCH")
        self.assertEqual(path.read_bytes(), installed)

    def test_state_symlink_is_refused(self):
        real = self.root / "real-state"
        real.mkdir(mode=0o700)
        self.state.symlink_to(real, target_is_directory=True)
        reply = startup.enable(self.state, home=self.home)
        self.assertEqual(reply["error"], "CONSOLE_LOGIN_SYMLINK_REFUSED")
        self.assertFalse((real / "console-login").exists())

    def test_status_has_no_write_side_effects(self):
        reply = self.assert_ok(startup.status(self.state, home=self.home))
        self.assertFalse(reply["enabled"])
        self.assertFalse(self.state.exists())
        self.assertFalse((self.home / ".framebridge-console-login.lock").exists())

    def test_default_home_uses_passwd_not_profile_environment_home(self):
        with mock.patch.object(startup.pwd, "getpwuid",
                               return_value=type("Identity", (), {"pw_dir": str(self.home)})()):
            with mock.patch.dict(os.environ, {"HOME": str(self.root / "wrong-home")}):
                reply = self.assert_ok(startup.enable(self.state))
        self.assertEqual(reply["startup_file"], str(self.home / ".profile"))

    def test_login_guards_and_quoted_state_are_executed_in_fixture_only(self):
        fixture_bin = self.root / "bin"
        fixture_bin.mkdir()
        tty = fixture_bin / "tty"
        tty.write_text("#!/bin/sh\nprintf '%s\\n' \"$" + "{FB_TEST_TTY-/dev/tty1}\"\n")
        tty.chmod(0o700)
        marker = self.root / "invocation.json"
        stub = fixture_bin / "fb-stub"
        stub.write_text("#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n"
                        + "Path(" + repr(str(marker)) + ").write_text(json.dumps(sys.argv[1:]))\n")
        stub.chmod(0o700)
        weird = self.root / "state with ' quote $(touch SHOULD_NOT_EXIST)"
        with mock.patch.object(startup, "FB_EXECUTABLE", str(stub)):
            self.assert_ok(startup.enable(weird, home=self.home))
        profile = self.home / ".profile"
        subprocess.run(["/bin/sh", "-n", str(profile)], check=True)
        base_env = dict(os.environ, PATH=str(fixture_bin) + ":" + os.environ["PATH"],
                        SSH_TTY="", TMUX="", FB_TEST_TTY="/dev/tty1")
        command = ["/bin/bash", "--noprofile", "--norc", "--login", "-c",
                   ". " + shlex.quote(str(profile))]
        for override in ({"SSH_TTY": "/dev/pts/9"}, {"TMUX": "test-client"},
                         {"FB_TEST_TTY": "/dev/tty2"}):
            subprocess.run(command, env=dict(base_env, **override), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.assertFalse(marker.exists())
        nonlogin = ["/bin/bash", "--noprofile", "--norc", "-c",
                    ". " + shlex.quote(str(profile))]
        subprocess.run(nonlogin, env=base_env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.assertFalse(marker.exists())
        subprocess.run(command, env=base_env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.assertEqual(json.loads(marker.read_text()),
                         ["--state-dir", str(weird), "console", "--mode", "tty"])
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())


class UserOnlyTests(unittest.TestCase):
    def test_root_context_is_rejected_before_any_path_change(self):
        with mock.patch.object(startup.os, "geteuid", return_value=0):
            reply = startup.enable("/does/not/exist", home="/also/not/used")
        self.assertEqual(reply["error"], "CONSOLE_LOGIN_USER_REQUIRED")


if __name__ == "__main__":
    unittest.main()
