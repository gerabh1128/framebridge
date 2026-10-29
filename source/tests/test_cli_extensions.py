"""Routing regression tests for package CLI additions; no external services."""
import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded import cli

class CliExtensionTests(unittest.TestCase):
    SID = "c" * 32

    def parsed(self, *parts):
        return cli.build_parser().parse_args(list(parts))

    def test_sudo_parser_keeps_node_and_console_variables(self):
        for mode in ("on", "status", "off"):
            args = self.parsed("tty", "sudo", mode, self.SID, "--node", "NodeCase")
            self.assertEqual((args.tty_command, args.sudo_mode, args.session_id, args.node),
                             ("sudo", mode, self.SID, "NodeCase"))

    def test_sudo_command_routes_exact_session_and_target(self):
        args = self.parsed("tty", "sudo", "on", self.SID, "--node", "NodeCase")
        with mock.patch.object(cli, "_target_tty", return_value={"ok": True}) as target, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.tty_command(args, Path("/qa/profile")), 0)
        target.assert_called_once_with(Path("/qa/profile"), "NodeCase",
            payload={"authorized": True, "op": "sudo", "session_id": self.SID, "mode": "on"})

    def test_remote_request_uses_profile_and_existing_fleet_target(self):
        seen = []
        def execute_node(node, **kwargs):
            seen.append((node, os.environ.get("FRAMEBRIDGE_EXPANDED_STATE"), kwargs))
            return {"ok": True}
        def request(execute, target, payload):
            return execute(target=target, script="printf bounded", authorized=True,
                           timeout_s=30, job_id="qa", sha256="d" * 64, include_stdout=True)
        modules = {
            "fleet": SimpleNamespace(execute_node=execute_node),
            "tty_mcp": SimpleNamespace(_remote_request=request),
        }
        with mock.patch.dict(os.environ, {"FRAMEBRIDGE_EXPANDED_STATE": "/previous/profile"}), \
             mock.patch.object(cli, "_module", side_effect=lambda name: modules[name]):
            value = cli._target_tty(Path("/qa/explicit"), "NodeCase", payload={"op": "observe"})
            self.assertEqual(os.environ["FRAMEBRIDGE_EXPANDED_STATE"], "/previous/profile")
        self.assertTrue(value["ok"])
        self.assertEqual(seen[0][:2], ("NodeCase", "/qa/explicit"))
        self.assertNotIn("target", seen[0][2])

    def test_remote_transport_failure_restores_absent_profile_without_local_fallback(self):
        modules = {
            "fleet": SimpleNamespace(execute_node=mock.Mock()),
            "tty_mcp": SimpleNamespace(_remote_request=mock.Mock(side_effect=RuntimeError("transport failed"))),
        }
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(cli, "_module", side_effect=lambda name: modules[name]):
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                cli._target_tty(Path("/qa/explicit"), "NodeCase", payload={"op": "status"})
            self.assertNotIn("FRAMEBRIDGE_EXPANDED_STATE", os.environ)
        modules["fleet"].execute_node.assert_not_called()

    def test_remote_service_action_uses_wire_cli_instead_of_local_daemon(self):
        seen = []
        def wire(execute, target, arguments):
            seen.append((target, arguments, os.environ.get("FRAMEBRIDGE_EXPANDED_STATE")))
            return {"ok": True}
        modules = {
            "fleet": SimpleNamespace(execute_node=mock.Mock()),
            "tty_mcp": SimpleNamespace(_exec_json=wire),
        }
        with mock.patch.object(cli, "_module", side_effect=lambda name: modules[name]):
            cli._target_tty(Path("/qa/profile"), "NodeCase", service="status")
        self.assertEqual(seen, [("NodeCase", ["status"], "/qa/profile")])

    def test_local_request_keeps_explicit_profile(self):
        request = mock.Mock(return_value={"ok": True})
        with mock.patch.object(cli, "_module", return_value=SimpleNamespace(request=request)):
            cli._target_tty(Path("/qa/profile"), "LOCAL", payload={"op": "status"})
        request.assert_called_once_with(Path("/qa/profile"), {"op": "status"})

    def test_failed_target_result_is_not_reported_as_success(self):
        args = self.parsed("tty", "close", self.SID, "--node", "NodeCase")
        with mock.patch.object(cli, "_target_tty", return_value={"ok": False, "error": "REMOTE_FAILED"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.tty_command(args, Path("/qa/profile")), 1)

    def test_text_transcript_observe_retains_node_argument(self):
        args = self.parsed("tty", "observe", self.SID, "--node", "NodeCase")
        with mock.patch.object(cli, "_target_tty", return_value={"ok": True}) as target, \
             contextlib.redirect_stdout(io.StringIO()):
            cli.tty_command(args, Path("/qa/profile"))
        self.assertEqual(target.call_args.kwargs["payload"]["op"], "observe")
        self.assertEqual(target.call_args.args[1], "NodeCase")

if __name__ == "__main__":
    unittest.main()
