import hashlib
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from framebridge_expanded import uninstall


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / ".config/systemd/user"
        self.directory.mkdir(parents=True)

    def unit(self, state=None):
        state = str(state or self.home / "state")
        name = "framebridge-autostart-" + hashlib.sha256(state.encode()).hexdigest()[:16] + ".service"
        path = self.directory / name
        quoted_state = state.replace("$", "$$").replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        path.write_text('# FrameBridge managed autostart v1\n[Service]\nEnvironment="PYTHONPATH=' + uninstall.MODULE_ROOT + '"\nExecStart="/usr/bin/python3" "-B" "-m" "framebridge_expanded.autostart" "_run" "--state" "' + quoted_state + '"\n[Install]\nWantedBy=default.target\n')
        return path

    def test_owned_service_matches_even_with_escaped_state(self):
        path = self.unit(self.home / "a space $ and % value")
        self.assertTrue(uninstall.managed_unit(path, os.getuid()))

    def test_wrong_owner_is_not_accepted(self):
        self.assertFalse(uninstall.managed_unit(self.unit(), os.getuid() + 1))

    def test_foreign_command_and_missing_marker_are_not_accepted(self):
        p = self.unit()
        p.write_text(p.read_text().replace('"framebridge_expanded.autostart"', '"another_application"'))
        self.assertFalse(uninstall.managed_unit(p, os.getuid()))
        p = self.unit();p.write_text(p.read_text().replace('# FrameBridge managed autostart v1', '# foreign'))
        self.assertFalse(uninstall.managed_unit(p, os.getuid()))

    def test_symlink_and_wrong_hash_are_not_accepted(self):
        p = self.unit();data = p.read_text();p.unlink();target = self.home / "outside";target.write_text(data);p.symlink_to(target)
        self.assertFalse(uninstall.managed_unit(p, os.getuid()))
        p.unlink();p.write_text(data);other = self.directory / ("framebridge-autostart-" + "f" * 16 + ".service");p.rename(other)
        self.assertFalse(uninstall.managed_unit(other, os.getuid()))

    def test_live_cleanup_stops_and_disables_only_owned_unit(self):
        p = self.unit();foreign = self.directory / "unrelated.service";foreign.write_text("unrelated")
        run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=b""))
        self.assertEqual(uninstall.cleanup_user(self.home, run=run, bus_available=True), 1)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls[0], ["/usr/bin/systemctl", "--user", "stop", p.name])
        self.assertEqual(calls[1], ["/usr/bin/systemctl", "--user", "--no-reload", "disable", p.name])
        self.assertEqual(foreign.read_text(), "unrelated")
        self.assertTrue(p.is_file())

    def test_actual_offline_disable_preserves_profile_and_cipher(self):
        p = self.unit();wants = self.directory / "default.target.wants";wants.mkdir();link = wants / p.name;link.symlink_to(Path("..") / p.name)
        profile = self.home / "profile.json";cipher = self.home / "credential.cred";profile.write_text('profile fixture');cipher.write_bytes(b'encrypted fixture only')
        self.assertEqual(uninstall.cleanup_user(self.home, bus_available=False), 1)
        self.assertFalse(link.is_symlink())
        self.assertEqual(profile.read_text(), 'profile fixture')
        self.assertEqual(cipher.read_bytes(), b'encrypted fixture only')
        self.assertTrue(p.is_file())

    def test_failed_stop_still_attempts_disable_and_reports_failure(self):
        self.unit()
        def invoke(args, **kwargs):
            if "stop" in args:return SimpleNamespace(returncode=1, stdout=b"")
            if "show" in args:return SimpleNamespace(returncode=0, stdout=b"active\n")
            return SimpleNamespace(returncode=0, stdout=b"")
        run = mock.Mock(side_effect=invoke)
        with self.assertRaises(RuntimeError):uninstall.cleanup_user(self.home, run=run, bus_available=True)
        self.assertTrue(any("disable" in c.args[0] for c in run.call_args_list))

    def test_no_managed_services_makes_no_calls(self):
        run = mock.Mock();self.assertEqual(uninstall.cleanup_user(self.home, run=run), 0);run.assert_not_called()

    def test_all_users_drops_privileges_and_includes_service_accounts(self):
        accounts = [SimpleNamespace(pw_name="testservice", pw_uid=104, pw_dir="/var/lib/testservice"), SimpleNamespace(pw_name="testperson", pw_uid=1000, pw_dir="/home/testperson"), SimpleNamespace(pw_name="nobody", pw_uid=65534, pw_dir="/nonexistent")]
        run = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with mock.patch.object(uninstall.os, "getuid", return_value=0):uninstall.cleanup_all_users(run=run, accounts=accounts)
        self.assertEqual(run.call_count, 2)
        for call, name in zip(run.call_args_list, ["testservice", "testperson"]):
            args = call.args[0]
            self.assertEqual(args[:7], ["/usr/sbin/runuser", "-u", name, "--", "/usr/bin/env", "-i", "HOME=" + ("/var/lib/testservice" if name == "testservice" else "/home/testperson")])
            self.assertEqual(args[-4:], ["-I", "-S", str(Path(uninstall.__file__).resolve()), "--user"])

    def test_root_dispatch_continues_after_one_user_failure(self):
        accounts = [SimpleNamespace(pw_name="first", pw_uid=1000, pw_dir="/home/first"), SimpleNamespace(pw_name="second", pw_uid=1001, pw_dir="/home/second")]
        run = mock.Mock(side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)])
        with mock.patch.object(uninstall.os, "getuid", return_value=0):
            with self.assertRaises(RuntimeError):uninstall.cleanup_all_users(run=run, accounts=accounts)
        self.assertEqual(run.call_count, 2)

    def test_nonroot_cannot_dispatch_other_users(self):
        with mock.patch.object(uninstall.os, "getuid", return_value=1000):
            with self.assertRaises(RuntimeError):uninstall.cleanup_all_users(accounts=[])

    def test_stop_timeout_still_disables_unit(self):
        self.unit()
        def invoke(args, **kwargs):
            if "stop" in args:raise subprocess.TimeoutExpired(args, 35)
            return SimpleNamespace(returncode=0, stdout=b"")
        run = mock.Mock(side_effect=invoke)
        with self.assertRaises(RuntimeError):uninstall.cleanup_user(self.home, run=run, bus_available=True)
        self.assertTrue(any("disable" in c.args[0] for c in run.call_args_list))
