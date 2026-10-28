import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded import console

class ConsoleTests(unittest.TestCase):
    def test_remote_alias_cannot_create_a_local_console(self):
        with tempfile.TemporaryDirectory() as path, mock.patch.object(Path, "mkdir") as mkdir:
            with self.assertRaisesRegex(RuntimeError, "REMOTE_CONSOLE_SSH_CONFIGURATION_REQUIRED"):
                console.context(Path(path) / "profile", "a-different-node", create=True)
            mkdir.assert_not_called()

    def test_profile_socket_and_job_roots_are_isolated(self):
        with tempfile.TemporaryDirectory() as path:
            one = console.context(Path(path) / "one")
            two = console.context(Path(path) / "two")
        self.assertNotEqual(one["socket"], two["socket"])
        self.assertNotEqual(one["session"], two["session"])
        self.assertEqual(one["jobs_root"], Path(path) / "one/home/.local/share/framebridge/fbterm")
        self.assertLess(len(str(one["socket"]).encode()), 108)

    def desktop(self, *, display=":0", vt="7", user=None):
        return SimpleNamespace(returncode=0, stdout=(
            "Type=x11\nUser=" + str(os.getuid() if user is None else user)
            + "\nActive=yes\nRemote=no\nDisplay=" + display + "\nVTNr=" + vt + "\nTTY=\nLeader=3668\n"))

    def test_lightdm_empty_tty_does_not_hide_active_user_desktop(self):
        with mock.patch.object(console.subprocess, "run", return_value=self.desktop()), \
             mock.patch.object(Path, "read_text", return_value="tty7\n"):
            self.assertTrue(console._active_graphical_display({"DISPLAY": ":0", "XDG_SESSION_ID": "19"}))

    def test_desktop_launcher_without_session_environment_uses_verified_own_display(self):
        with mock.patch.object(console.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout="19\n"), self.desktop()]) as run, \
             mock.patch.object(Path, "read_text", return_value="tty7\n"):
            self.assertTrue(console._active_graphical_display({"DISPLAY": ":0"}))
        self.assertEqual(run.call_args_list[0].args[0], [
            "loginctl", "show-user", str(os.getuid()), "--property=Display", "--value"])
        self.assertIn("19", run.call_args_list[1].args[0])

    def test_missing_primary_session_does_not_guess_a_display_session(self):
        with mock.patch.object(console.subprocess, "run", return_value=
                SimpleNamespace(returncode=0, stdout="\n")) as run:
            self.assertFalse(console._active_graphical_display({"DISPLAY": ":0"}))
        run.assert_called_once()

    def test_invalid_supplied_session_is_not_replaced(self):
        with mock.patch.object(console.subprocess, "run") as run:
            self.assertFalse(console._active_graphical_display({"DISPLAY": ":0", "XDG_SESSION_ID": "../bad"}))
        run.assert_not_called()

    def test_private_virtual_display_is_not_chosen_as_user_desktop(self):
        with mock.patch.object(console.subprocess, "run", return_value=self.desktop()), \
             mock.patch.object(Path, "read_text", return_value="tty7\n"):
            self.assertFalse(console._active_graphical_display({"DISPLAY": ":99", "XDG_SESSION_ID": "19"}))

    def test_changed_foreground_vt_prevents_gui_auto_open(self):
        with mock.patch.object(console.subprocess, "run", return_value=self.desktop()), \
             mock.patch.object(Path, "read_text", return_value="tty1\n"):
            self.assertFalse(console._active_graphical_display({"DISPLAY": ":0", "XDG_SESSION_ID": "19"}))

    def test_foreign_user_session_is_not_selected(self):
        with mock.patch.object(console.subprocess, "run", return_value=self.desktop(user=os.getuid() + 1)), \
             mock.patch.object(Path, "read_text", return_value="tty7\n"):
            self.assertFalse(console._active_graphical_display({"DISPLAY": ":0", "XDG_SESSION_ID": "19"}))

    def test_noninteractive_terminal_fails_before_starting_a_console(self):
        with tempfile.TemporaryDirectory() as path:
            ctx = console.context(Path(path) / "one")
            with mock.patch.object(console, "context", return_value=ctx), \
                 mock.patch.object(console, "_active_graphical_display", return_value=False), \
                 mock.patch.object(console.os, "isatty", return_value=False), \
                 mock.patch.object(console.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "CONSOLE_INTERACTIVE_TERMINAL_REQUIRED"):
                    console.run(ctx["state"])
                run.assert_not_called()

    def test_terminal_attach_keeps_shared_identity_and_profile_jobs(self):
        with tempfile.TemporaryDirectory() as path:
            ctx = console.context(Path(path) / "one")
            with mock.patch.object(console, "context", return_value=ctx), \
                 mock.patch.object(console, "_active_graphical_display", return_value=False), \
                 mock.patch.object(console.os, "isatty", return_value=True), \
                 mock.patch.object(console.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                 mock.patch.object(console.os, "execve") as execute:
                self.assertEqual(console.run(ctx["state"], inside=True), 0)
                program, args, env = execute.call_args.args
        self.assertEqual(program, "/usr/bin/tmux")
        self.assertIn(str(ctx["socket"]), args)
        self.assertIn("=" + ctx["session"], args)
        self.assertEqual(env["FRAMEBRIDGE_FBTERM_ROOT"], str(ctx["jobs_root"]))
        self.assertNotIn("sudo", args)

    def test_layout_failure_does_not_attach_or_replace_a_shell(self):
        with tempfile.TemporaryDirectory() as path:
            ctx = console.context(Path(path) / "one")
            with mock.patch.object(console, "context", return_value=ctx), \
                 mock.patch.object(console, "_active_graphical_display", return_value=False), \
                 mock.patch.object(console.os, "isatty", return_value=True), \
                 mock.patch.object(console.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
                 mock.patch.object(console.os, "execve") as execute:
                with self.assertRaisesRegex(RuntimeError, "CONSOLE_LAYOUT_FAILED"):
                    console.run(ctx["state"], inside=True)
                execute.assert_not_called()

    def test_dangling_socket_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as path:
            sock = Path(path) / "console.sock"
            sock.symlink_to(Path(path) / "missing")
            with self.assertRaisesRegex(RuntimeError, "CONSOLE_SOCKET_UNTRUSTED"):
                console._socket_exists(sock)

    def test_public_runtime_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as path:
            os.chmod(path, 0o755)
            with self.assertRaisesRegex(RuntimeError, "CONSOLE_RUNTIME_DIRECTORY_UNTRUSTED"):
                console._private_runtime(Path(path))

if __name__ == "__main__":
    unittest.main()
