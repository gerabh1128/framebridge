"""Runtime stop must propagate terminal cleanup evidence."""
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import call, patch

from framebridge_expanded import runtime, tty


class RuntimeTerminalCleanupTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.state = Path("/unused-framebridge-runtime-test")
        stack.enter_context(patch.object(runtime, "state_path", return_value=self.state))
        stack.enter_context(patch.object(runtime, "load_config", return_value={}))
        self.disconnect = stack.enter_context(patch(
            "framebridge_expanded.connection_background.disconnect", return_value={"ok": True}))
        self.manual = stack.enter_context(patch("framebridge_expanded.autostart.mark_manual_stop"))
        self.terminal = stack.enter_context(patch.object(
            tty, "stop", return_value={"ok": True, "cleanup_verified": True}))
        self.manager = {"pid": 101}
        self.child = {"pid": 102}
        self.worker = {"pid": 103}
        self.read = stack.enter_context(patch.object(runtime, "_read", side_effect=[
            self.manager, {"processes": [self.child]}, {"processes": [self.worker]}]))
        self.terminate = stack.enter_context(patch.object(runtime, "_terminate"))
        self.atomic = stack.enter_context(patch.object(runtime, "_atomic"))
        self.status = stack.enter_context(patch.object(
            runtime, "status", return_value={"ok": True, "running": False, "ready": False}))

    def test_failed_terminal_cleanup_leaves_local_services_running(self):
        self.terminal.return_value = {"ok": False, "state": "STOPPED", "cleanup_verified": False}
        with self.assertRaisesRegex(RuntimeError, "TTY cleanup could not be verified"):
            runtime.stop(self.state)
        self.disconnect.assert_called_once_with(self.state)
        self.manual.assert_not_called()
        self.terminate.assert_not_called()
        self.atomic.assert_not_called()
        self.status.assert_not_called()

    def test_cleanup_requires_both_explicit_true_fields(self):
        for outcome in ({"ok": True}, {"ok": True, "cleanup_verified": False},
                        {"ok": False, "cleanup_verified": True}, None):
            with self.subTest(outcome=outcome):
                self.terminal.return_value = outcome
                with self.assertRaises(RuntimeError):
                    runtime.stop(self.state)
        self.terminate.assert_not_called()

    def test_normal_stop_verifies_before_and_after_terminating_services(self):
        result = runtime.stop(self.state)
        self.assertEqual(self.terminal.call_args_list, [call(self.state), call(self.state)])
        self.terminate.assert_has_calls([
            call(None, self.manager), call(None, self.child), call(None, self.worker)])
        self.manual.assert_called_once_with(self.state)
        self.assertTrue(result["tty_cleanup_verified"])
        self.assertFalse(result["running"])

    def test_late_cleanup_failure_cannot_return_global_success(self):
        self.terminal.side_effect = [
            {"ok": True, "cleanup_verified": True},
            {"ok": False, "cleanup_verified": False}]
        with self.assertRaisesRegex(RuntimeError, "TTY cleanup could not be verified"):
            runtime.stop(self.state)
        self.assertTrue(self.terminate.called)
        self.atomic.assert_called_once_with(self.state / "ready.json", {"ready": False})
        self.status.assert_not_called()

    def test_failed_disconnect_preserves_existing_stop_guard(self):
        self.disconnect.return_value = {"ok": False}
        with self.assertRaisesRegex(RuntimeError, "background ChatGPT connection"):
            runtime.stop(self.state)
        self.terminal.assert_not_called()
        self.manual.assert_not_called()
        self.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
