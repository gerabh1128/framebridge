import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded import console


class ConsoleNestingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.ctx = {
            "state": root / "profile", "node": "test-node", "runtime": root,
            "socket": root / "console.sock", "session": "FB-test-profile",
            "jobs_root": root / "profile/home/.local/share/framebridge/fbterm",
        }

    def environment(self, *, socket=None, pane="%0"):
        return {"TMUX": str(socket or self.ctx["socket"]) + ",1234,0",
                "TMUX_PANE": pane}

    def invoke(self, env, query=None, *, graphical=False):
        def respond(args, **kwargs):
            if "list-panes" in args:
                if isinstance(query, Exception):
                    raise query
                return query or SimpleNamespace(returncode=0, stdout="%0\n%1\n")
            return SimpleNamespace(returncode=0, stdout="")
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(console, "context", return_value=self.ctx), \
             mock.patch.object(console, "_active_graphical_display", return_value=graphical), \
             mock.patch.object(console.os, "isatty", return_value=True), \
             mock.patch.object(console.shutil, "which", return_value="/usr/bin/gnome-terminal"), \
             mock.patch.object(console.subprocess, "run", side_effect=respond) as run, \
             mock.patch.object(console.os, "execve") as execute:
            try:
                result = console.run(self.ctx["state"], mode="gui" if graphical else "tty")
            except RuntimeError as error:
                result = error
        return result, run, execute

    def assert_blocked_before_layout(self, result, run, execute, error):
        self.assertIsInstance(result, RuntimeError)
        self.assertIn(error, str(result))
        execute.assert_not_called()
        for call in run.call_args_list:
            self.assertIn("list-panes", call.args[0])
            self.assertNotIn("--apply", call.args[0])

    def test_same_console_pane_is_rejected_before_layout_and_attach(self):
        result, run, execute = self.invoke(self.environment())
        self.assert_blocked_before_layout(result, run, execute, "CONSOLE_ALREADY_ATTACHED")
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertIn("=" + self.ctx["session"], args)
        self.assertEqual(args[-2:], ["-F", "#{pane_id}"])

    def test_socket_path_alias_does_not_bypass_self_attach_check(self):
        alias = str(self.ctx["socket"].parent) + "/./console.sock"
        env = {"TMUX": alias + ",1234,0", "TMUX_PANE": "%0"}
        result, run, execute = self.invoke(env)
        self.assert_blocked_before_layout(result, run, execute, "CONSOLE_ALREADY_ATTACHED")

    def test_unrelated_profile_can_attach_normally(self):
        result, run, execute = self.invoke(self.environment(socket=self.ctx["socket"].with_name("other.sock")))
        self.assertEqual(result, 0)
        execute.assert_called_once()
        run.assert_called_once()
        self.assertIn("--apply", run.call_args.args[0])

    def test_other_session_without_shared_panes_can_attach(self):
        result, run, execute = self.invoke(self.environment(pane="%9"))
        self.assertEqual(result, 0)
        execute.assert_called_once()
        self.assertEqual(run.call_count, 2)
        self.assertIn("--apply", run.call_args.args[0])

    def test_plain_terminal_can_attach_normally(self):
        result, run, execute = self.invoke({})
        self.assertEqual(result, 0)
        execute.assert_called_once()
        run.assert_called_once()
        self.assertIn("--apply", run.call_args.args[0])

    def test_failed_membership_query_does_not_assume_attach_is_safe(self):
        result, run, execute = self.invoke(
            self.environment(), SimpleNamespace(returncode=1, stdout=""))
        self.assert_blocked_before_layout(result, run, execute, "CONSOLE_TMUX_CONTEXT_UNVERIFIED")

    def test_query_timeout_does_not_attach(self):
        result, run, execute = self.invoke(
            self.environment(), subprocess.TimeoutExpired(["tmux"], 5))
        self.assert_blocked_before_layout(result, run, execute, "CONSOLE_TMUX_CONTEXT_UNVERIFIED")

    def test_invalid_pane_metadata_does_not_attach(self):
        result, run, execute = self.invoke(
            self.environment(), SimpleNamespace(returncode=0, stdout="not-a-pane\n"))
        self.assert_blocked_before_layout(result, run, execute, "CONSOLE_TMUX_CONTEXT_UNVERIFIED")

    def test_malformed_matching_context_does_not_run_tmux(self):
        result, run, execute = self.invoke(self.environment(pane="invalid"))
        self.assert_blocked_before_layout(result, run, execute, "CONSOLE_TMUX_CONTEXT_UNVERIFIED")
        run.assert_not_called()

    def test_independent_gui_window_keeps_clean_environment(self):
        result, run, execute = self.invoke(self.environment(), graphical=True)
        self.assertEqual(result, 0)
        execute.assert_not_called()
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "/usr/bin/gnome-terminal")
        self.assertNotIn("TMUX", kwargs["env"])
        self.assertNotIn("TMUX_PANE", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
