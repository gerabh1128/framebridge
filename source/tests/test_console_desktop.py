import hashlib
import os
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded import console_desktop
from framebridge_expanded.console_ui import framebridge_console as ui


@unittest.skipIf(os.getuid() == 0, "desktop migration requires a normal user")
class DesktopMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.home / "profile"
        self.runtime = root / "runtime"
        self.runtime.mkdir(mode=0o700)
        parent = self.runtime / "testhost-tty1"
        parent.mkdir(mode=0o700)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(parent / "console.sock"))
        os.chmod(parent / "console.sock", 0o600)
        self.addCleanup(self.sock.close)
        self.desktop = self.home / ".local/share/applications/framebridge-console.desktop"
        self.desktop.parent.mkdir(parents=True, mode=0o700)
        self.desktop.write_bytes(console_desktop._legacy_entry(self.home))
        self.wrapper = self.home / ".local/bin/framebridge-console"
        self.wrapper.parent.mkdir(parents=True, mode=0o700)
        self.wrapper.write_bytes(b"#!/usr/bin/python3\npass\n")
        self.old_hash = hashlib.sha256(self.wrapper.read_bytes()).hexdigest()

    def migrate(self, windows="@0|Console\n@1|FB-piloto\n@3|FB-Muffin\n"):
        def result(args, **kwargs):
            text = windows if "list-windows" in args else "bash|0|shell\npython3|0|monitor\n"
            return SimpleNamespace(returncode=0, stdout=text)
        with mock.patch.object(console_desktop, "LEGACY_WRAPPER_SHA256", self.old_hash), \
             mock.patch.object(console_desktop.subprocess, "run", side_effect=result) as run:
            value = console_desktop.migrate_legacy(
                self.state, home=self.home, node="TestHost", runtime=self.runtime)
        return value, run

    def test_known_legacy_icon_keeps_existing_socket_and_session(self):
        before = self.desktop.read_bytes()
        value, run = self.migrate()
        self.assertTrue(value["changed"])
        self.assertFalse(value["runtime_changed"])
        self.assertEqual((self.state / "console-desktop-legacy.backup").read_bytes(), before)
        self.assertEqual(self.desktop.read_bytes(), console_desktop._new_entry(self.state))
        self.assertEqual(self.wrapper.read_bytes(), console_desktop._new_wrapper(self.state))
        self.assertEqual((self.state / "console-wrapper-legacy.backup").read_bytes(), b"#!/usr/bin/python3\npass\n")
        self.assertTrue(value["wrapper_changed"])
        context = console_desktop.legacy_context(self.state, "TestHost", self.runtime)
        self.assertEqual(context["session"], "TESTHOST-FBTERM")
        self.assertEqual(context["socket"], self.runtime / "testhost-tty1/console.sock")
        self.assertEqual(run.call_count, 2)
        self.assertIn("list-windows", run.call_args_list[0].args[0])
        self.assertIn("list-panes", run.call_args_list[1].args[0])
        second, run = self.migrate()
        self.assertFalse(second["changed"])
        run.assert_not_called()

    def test_custom_desktop_entry_is_preserved(self):
        self.desktop.write_text("[Desktop Entry]\nExec=my-custom-console\n")
        before = self.desktop.read_bytes()
        value, run = self.migrate()
        self.assertEqual(value["reason"], "CUSTOM_DESKTOP_ENTRY_PRESERVED")
        self.assertEqual(self.desktop.read_bytes(), before)
        self.assertFalse(self.state.exists())
        run.assert_not_called()

    def test_changed_legacy_wrapper_does_not_get_replaced(self):
        self.wrapper.write_text("custom owner command\n")
        before = self.desktop.read_bytes()
        with self.assertRaisesRegex(ValueError, "LEGACY_WRAPPER_CHANGED"):
            self.migrate()
        self.assertEqual(self.desktop.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_ambiguous_console_window_prevents_migration(self):
        before = self.desktop.read_bytes()
        with self.assertRaisesRegex(ValueError, "LEGACY_WINDOW_UNVERIFIED"):
            self.migrate("@0|Console\n@1|Console\n")
        self.assertEqual(self.desktop.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_symlinked_entry_is_refused_without_touching_target(self):
        self.desktop.unlink()
        target = self.home / "owner-file"
        target.write_text("keep\n")
        self.desktop.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "SYMLINK_REFUSED"):
            self.migrate()
        self.assertEqual(target.read_text(), "keep\n")

    def test_foreign_profile_cannot_reuse_the_binding(self):
        self.migrate()
        record = self.state / console_desktop.RECORD
        raw = record.read_text().replace(str(self.state), str(self.state) + "-other")
        record.write_text(raw)
        with self.assertRaisesRegex(ValueError, "RECORD_INVALID"):
            console_desktop.legacy_context(self.state, "TestHost", self.runtime)


class ConsoleWindowSelectionTests(unittest.TestCase):
    def console(self, windows):
        console = ui.Console(SimpleNamespace(socket="/no-real-socket", session="NODE-FBTERM"))
        def tmux(*args, **kwargs):
            if args[0] == "list-sessions":
                return "NODE-FBTERM|$0"
            if args[0] == "list-windows":
                return windows
            raise AssertionError(args)
        with mock.patch.object(ui.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(console, "tmux", side_effect=tmux):
            value = console.exists()
        return console, value

    def test_migrated_wrapper_targets_the_packaged_profile_without_a_shell(self):
        state = Path("/home/user/profile")
        with mock.patch("sys.argv", ["framebridge-console"]), mock.patch("os.execv") as execute:
            exec(compile(console_desktop._new_wrapper(state), "wrapper", "exec"), {})
        execute.assert_called_once_with("/usr/bin/fb", ["/usr/bin/fb", "--state-dir", str(state), "console"])

    def test_migrated_inside_wrapper_does_not_create_another_viewer(self):
        state = Path("/home/user/profile")
        with mock.patch("sys.argv", ["framebridge-console", "--inside"]), mock.patch("os.execv") as execute:
            exec(compile(console_desktop._new_wrapper(state), "wrapper", "exec"), {})
        self.assertEqual(execute.call_args.args[1][-3:], ["--inside", "--mode", "gui"])

    def test_monitor_receives_profile_jobs_root_as_an_explicit_argument(self):
        args = SimpleNamespace(socket="/owned/socket", session="NODE-FBTERM", node="NODE",
                               jobs_root=Path("/profile with space/jobs"))
        command = shlex.split(ui.Console(args).monitor_command())
        self.assertEqual(command[-2:], ["--jobs-root", "/profile with space/jobs"])

    def test_desktop_entry_escapes_percent_field_codes(self):
        entry = console_desktop._new_entry(Path("/home/user/100%complete/profile"))
        self.assertIn(b"100%%complete", entry)

    def test_desktop_entry_refuses_newline_in_profile_path(self):
        with self.assertRaisesRegex(ValueError, "STATE_PATH_INVALID"):
            console_desktop._new_entry(Path("/home/user/profile\nExec=bad"))

    def test_extra_owner_windows_do_not_prevent_console_attachment(self):
        console, value = self.console("@0|Console\n@1|FB-piloto\n@3|FB-Muffin")
        self.assertTrue(value)
        self.assertEqual(console.window, "@0")
        self.assertEqual(console.session_target, "$0")

    def test_ambiguous_console_window_is_not_guessed(self):
        with self.assertRaisesRegex(RuntimeError, "absent or ambiguous"):
            self.console("@0|Console\n@1|Console")

    def test_multiwindow_session_without_console_is_preserved(self):
        with self.assertRaisesRegex(RuntimeError, "absent or ambiguous"):
            self.console("@0|Owner work\n@1|Other")


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class RealConsolePreservationTests(unittest.TestCase):
    def test_apply_preserves_shell_and_other_windows_in_existing_session(self):
        with tempfile.TemporaryDirectory() as directory:
            sock = str(Path(directory) / "console.sock")
            def tmux(*args):
                return subprocess.check_output(["/usr/bin/tmux", "-S", sock, *args], text=True).strip()
            try:
                tmux("new-session", "-d", "-s", "TEST-FBTERM", "-n", "Console", "/bin/bash --noprofile --norc")
                shell = tmux("display-message", "-p", "-t", "=TEST-FBTERM:Console", "#{pane_pid}")
                tmux("new-window", "-d", "-t", "=TEST-FBTERM", "-n", "FB-piloto", "/bin/bash --noprofile --norc")
                tmux("new-window", "-d", "-t", "=TEST-FBTERM", "-n", "FB-Muffin", "/bin/bash --noprofile --norc")
                fmt = "#{window_name}|#{pane_id}|#{pane_pid}"
                other = tmux("list-panes", "-a", "-F", fmt).splitlines()[1:]
                jobs = Path(directory) / "profile-jobs"
                args = SimpleNamespace(socket=sock, session="TEST-FBTERM", node="Test", jobs_root=jobs,
                                       layout="horizontal", reflow=False, refresh_monitor=False)
                ui.Console(args).apply()
                panes = tmux("list-panes", "-t", "=TEST-FBTERM:Console", "-F", "#{pane_pid}|#{@framebridge_role}")
                self.assertIn(shell + "|shell", panes)
                start_commands = tmux("list-panes", "-t", "=TEST-FBTERM:Console", "-F", "#{pane_start_command}")
                self.assertIn("--jobs-root", start_commands)
                self.assertIn(str(jobs), start_commands)
                after = tmux("list-panes", "-a", "-F", fmt).splitlines()
                for row in other:
                    self.assertIn(row, after)
                self.assertEqual(tmux("list-windows", "-t", "=TEST-FBTERM", "-F", "#{window_name}"),
                                 "Console\nFB-piloto\nFB-Muffin")
            finally:
                subprocess.run(["/usr/bin/tmux", "-S", sock, "kill-server"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
