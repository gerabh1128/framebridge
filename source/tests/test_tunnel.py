import io
import os
from pathlib import Path
import signal
import sys
import time
import subprocess
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import tunnel

ID = "tunnel_0123456789abcdef0123456789abcdef"
ROOT = Path(__file__).resolve().parents[1]


class ParserTests(unittest.TestCase):
    def test_id_and_documented_commands(self):
        commands = [
            ID,
            "'" + ID + "'",
            "tunnel-client run --control-plane.tunnel-id " + ID,
            "/opt/client/tunnel-client run --control-plane.tunnel-id=" + ID + " --control-plane.api-key env:OLD_KEY --mcp.server-url url=http://127.0.0.1:8898/mcp",
            "tunnel-client init --profile demo --sample sample_mcp_remote_no_auth --tunnel-id " + ID + " --mcp-server-url http://127.0.0.1:8898/mcp",
            "tunnel-client runtimes connect --alias demo --tunnel-id " + ID + " --runtime-api-key env:OLD_KEY --json",
            "framebridge-original tunnel -- run --control-plane.tunnel-id " + ID,
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(tunnel.parse_tunnel_input(command).tunnel_id, ID)

    def test_rejections_never_echo_hidden_input(self):
        secret = "sk-test-DO_NOT_DISCLOSE-0123456789"
        bad = [
            "sudo tunnel-client run --control-plane.tunnel-id " + ID,
            "env OPENAI_API_KEY=" + secret + " tunnel-client run --control-plane.tunnel-id " + ID,
            "tunnel-client run --control-plane.tunnel-id " + ID + "; echo bad",
            "tunnel-client run --control-plane.tunnel-id $(" + secret + ")",
            "tunnel-client run --control-plane.tunnel-id " + ID + " | cat",
            "tunnel-client run --control-plane.tunnel-id " + ID + " --config /private/profile.yaml",
            "tunnel-client run --control-plane.tunnel-id " + ID + " --control-plane.api-key " + secret,
            "tunnel-client run --control-plane.tunnel-id " + ID + " --control-plane.api-key file:/private/key",
            "tunnel-client run --control-plane.tunnel-id " + ID + " --control-plane.base-url https://example.invalid",
            "tunnel-client run --control-plane.tunnel-id " + ID + " --tunnel-id " + ID,
            "tunnel-client run --control-plane.tunnel-id",
            "tunnel-client run --control-plane.tunnel-id '" + secret,
        ]
        for command in bad:
            with self.subTest(command=command):
                with self.assertRaises(tunnel.TunnelInputError) as error:
                    tunnel.parse_tunnel_input(command)
                self.assertNotIn(secret, str(error.exception))
                self.assertNotIn(command, str(error.exception))

    def test_rebuild_uses_packaged_binary_and_profile_endpoint(self):
        spec = tunnel.parse_tunnel_input("tunnel-client run --control-plane.tunnel-id " + ID + " --mcp.server-url https://mcp.example.com/mcp")
        args = tunnel.build_command("/packaged/tunnel-client", "/own/state", "http://127.0.0.1:20865/mcp", spec)
        self.assertEqual(args[0], "/packaged/tunnel-client")
        self.assertIn("url=http://127.0.0.1:20865/mcp", args)
        self.assertNotIn("https://mcp.example.com/mcp", args)
        self.assertIn("env:" + tunnel.KEY_ENV, args)

    def test_inherited_runtime_settings_removed_without_global_mutation(self):
        original = {"PATH": "/usr/bin", "DISPLAY": ":77", "HTTPS_PROXY": "http://proxy.example", "CA_BUNDLE": "/ca.pem",
                    "TUNNEL_CLIENT_CONFIG": "/production/config", "CONTROL_PLANE_TUNNEL_ID": "production",
                    "MCP_SERVER_URL": "https://production", "MCP_COMMAND": "production-command",
                    "HEALTH_URL_FILE": "/production/health", "LOG_HTTP_RAW_UNSAFE": "true",
                    "CLOUDFLARED_TUNNEL_TOKEN": "not-used", "OPENAI_API_KEY": "not-used"}
        snapshot = dict(original)
        env = tunnel.child_environment("FAKE_RUNTIME_KEY", original)
        self.assertEqual(original, snapshot)
        for key in snapshot:
            if key.startswith(tunnel._RUNTIME_PREFIXES):
                self.assertNotIn(key, env)
        self.assertEqual(env["HTTPS_PROXY"], original["HTTPS_PROXY"])
        self.assertEqual(env[tunnel.KEY_ENV], "FAKE_RUNTIME_KEY")

    def test_child_default_profile_paths_are_private_and_scoped(self):
        with tempfile.TemporaryDirectory(prefix="tunnel-test-", dir=ROOT / "qa") as td:
            env = tunnel.child_environment("FAKE_ONLY", {"HOME": "/old/home", "XDG_CONFIG_HOME": "/old/config"}, state=Path(td))
            self.assertEqual(env["HOME"], str(Path(td) / "chatgpt-home"))
            self.assertEqual(env["XDG_CONFIG_HOME"], str(Path(td) / "chatgpt-home/.config"))
            self.assertEqual(Path(env["TMPDIR"]).stat().st_mode & 0o777, 0o700)
            self.assertFalse(any(p.is_file() for p in Path(td).rglob("*")))

    def test_saved_public_context_is_reused(self):
        public = {"tunnel_id": ID, "control_plane_organization_id": "org_test", "control_plane_url_path": "/v1/tunnel"}
        with mock.patch.object(tunnel, "_hidden", side_effect=["", "FAKE_ONLY"]), mock.patch("framebridge_expanded.connection_background.run_foreground", return_value=0) as run:
            rc = tunnel.connect_chatgpt("/binary", "/state", "http://127.0.0.1:20865/mcp", public, output=io.StringIO(), foreground=True)
        self.assertEqual(rc, 0)
        self.assertIn("org_test", run.call_args.args[0])
        self.assertIn("/v1/tunnel", run.call_args.args[0])

    def test_non_tty_fails_before_secret_prompt(self):
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=False), mock.patch.object(tunnel.getpass, "getpass") as prompt:
            with self.assertRaises(tunnel.TunnelInputError):
                tunnel._hidden("Hidden: ")
        prompt.assert_not_called()


class ForegroundTests(unittest.TestCase):
    def command(self, code):
        return [sys.executable, "-u", "-c", code,
                "--control-plane.tunnel-id", ID,
                "--mcp.server-url", "url=http://127.0.0.1:20865/mcp"]

    def test_fake_key_only_in_child_environment_and_private_log(self):
        secret = "FAKE_RUNTIME_ONLY_0123456789"
        seen = {}
        real_popen = subprocess.Popen
        def spawn(args, **kwargs):
            seen["args"] = list(args)
            seen["env"] = dict(kwargs["env"])
            return real_popen(args, **kwargs)
        code = "import os,time;print('TECHNICAL_DEBUG '+os.environ['" + tunnel.KEY_ENV + "'],flush=True);time.sleep(.3)"
        with tempfile.TemporaryDirectory(prefix="tunnel-output-") as td:
            output = io.StringIO()
            with mock.patch("framebridge_expanded.connection_monitor.probe", return_value=("ready", "Verified synthetic test state")):
                result = tunnel.run_foreground(self.command(code), secret, state=td, output=output, popen=spawn)
            data = (Path(td) / "logs/chatgpt/connection.log").read_text()
            self.assertEqual(result, 0)
            self.assertEqual(seen["env"][tunnel.KEY_ENV], secret)
            self.assertNotIn(secret, repr(seen["args"]))
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn("TECHNICAL_DEBUG", output.getvalue())
            self.assertIn("TECHNICAL_DEBUG [REDACTED]", data)
            self.assertNotIn(secret, data)
            self.assertIn("[OK]", output.getvalue())
            self.assertIn("successful tool call", output.getvalue())
            self.assertEqual((Path(td) / "logs/chatgpt/connection.log").stat().st_mode & 0o777, 0o600)

    def test_ctrl_c_stops_only_owned_child(self):
        real_popen = subprocess.Popen
        seen = {}
        def spawn(args, **kwargs):
            seen["process"] = real_popen(args, **kwargs)
            return seen["process"]
        with tempfile.TemporaryDirectory(prefix="tunnel-stop-") as td:
            with mock.patch("framebridge_expanded.connection_monitor.probe", side_effect=KeyboardInterrupt):
                rc = tunnel.run_foreground(self.command("import time;time.sleep(60)"), "FAKE_ONLY", state=td,
                                           output=io.StringIO(), popen=spawn)
            self.assertEqual(rc, 130)
            self.assertIsNotNone(seen["process"].poll())
            self.assertFalse(any((Path(td) / "connection-runtime").iterdir()))

    def test_interactive_flow_saves_only_public_values(self):
        secret = "FAKE_RUNTIME_ONLY_0123456789"
        saved = []
        output = io.StringIO()
        with mock.patch.object(tunnel, "_hidden", side_effect=[ID, secret]), mock.patch("framebridge_expanded.connection_background.run_foreground", return_value=0) as run:
            rc = tunnel.connect_chatgpt("/binary", "/state", "http://127.0.0.1:20865/mcp", {"account_label": "test@example.com"}, saved.append, output=output, foreground=True)
        self.assertEqual(rc, 0)
        self.assertEqual(saved[0]["tunnel_id"], ID)
        self.assertNotIn(secret, repr(saved))
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual(run.call_args.args[1], secret)
        self.assertNotIn(secret, repr(run.call_args.args[0]))


class ManagedWizardTests(unittest.TestCase):
    @staticmethod
    def ready():
        return {"configured": True, "enabled": True, "service_active": True,
                "service_state": "active", "runtime": {"running": True, "ready": True},
                "connection": {"alive": True, "verified": True, "state": "ready"}}

    def invoke(self, configure, *, public=None, output=None, wait_timeout=0):
        return tunnel.connect_chatgpt("/binary", "/state", "http://127.0.0.1:20865/mcp", public,
                                     output=output or io.StringIO(), configure_connection=configure,
                                     wait_timeout=wait_timeout)

    def test_three_fields_are_ordered_and_only_public_settings_are_committed(self):
        events = []
        def label(prompt):
            events.append(prompt)
            return "test@example.com"
        answers = iter([ID, "SYNTHETIC-MCP-ACCESS-KEY"])
        def hidden(prompt):
            events.append(prompt)
            return next(answers)
        seen = []
        def configure(state, public, credential):
            events.append("commit")
            seen.append((state, public, credential))
            return self.ready()
        output = io.StringIO()
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", side_effect=label), mock.patch.object(tunnel, "_hidden", side_effect=hidden):
            rc = self.invoke(configure, output=output)
        self.assertEqual(0, rc)
        self.assertTrue(events[0].startswith("ChatGPT account (email label)"))
        self.assertTrue(events[1].startswith("Tunnel ID (hidden"))
        self.assertEqual("MCP server access key (hidden): ", events[2])
        self.assertEqual("commit", events[3])
        self.assertEqual("test@example.com", seen[0][1]["account_label"])
        self.assertEqual(ID, seen[0][1]["tunnel_id"])
        self.assertNotIn("SYNTHETIC-MCP-ACCESS-KEY", repr(seen[0][1]))
        self.assertNotIn("SYNTHETIC-MCP-ACCESS-KEY", output.getvalue())
        self.assertEqual("Connecting...\n[OK] Connected.\n", output.getvalue())

    def test_cancel_at_each_field_never_calls_committer(self):
        for field in range(3):
            with self.subTest(field=field):
                configure = mock.Mock()
                label = mock.Mock(return_value="test@example.com")
                hidden = mock.Mock(side_effect=[ID, "SYNTHETIC-MCP-ACCESS-KEY"])
                if field == 0:
                    label.side_effect = KeyboardInterrupt
                elif field == 1:
                    hidden.side_effect = KeyboardInterrupt
                else:
                    hidden.side_effect = [ID, KeyboardInterrupt]
                with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", label), mock.patch.object(tunnel, "_hidden", hidden):
                    rc = self.invoke(configure)
                self.assertEqual(130, rc)
                configure.assert_not_called()

    def test_full_command_in_normal_hidden_id_field_is_rejected_without_echo(self):
        configure = mock.Mock()
        pasted = "tunnel-client run --tunnel-id " + ID + " --api-key SYNTHETIC-PASTED-SECRET"
        output = io.StringIO()
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="test@example.com"), mock.patch.object(tunnel, "_hidden", return_value=pasted) as hidden:
            with self.assertRaises(tunnel.TunnelInputError) as error:
                self.invoke(configure, output=output)
        self.assertNotIn("SYNTHETIC-PASTED-SECRET", str(error.exception) + output.getvalue())
        self.assertEqual(1, hidden.call_count)
        configure.assert_not_called()

    def test_invalid_key_does_not_commit_and_is_not_echoed(self):
        configure = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="test@example.com"), mock.patch.object(tunnel, "_hidden", side_effect=[ID, "SYNTHETIC INVALID KEY"]):
            with self.assertRaises(tunnel.TunnelInputError) as error:
                self.invoke(configure, output=output)
        self.assertNotIn("SYNTHETIC INVALID KEY", str(error.exception) + output.getvalue())
        configure.assert_not_called()

    def test_saved_public_defaults_preserve_tunnel_context_but_key_is_requested(self):
        public = {"account_label": "test@example.com", "tunnel_id": ID,
                  "control_plane_organization_id": "org_test", "control_plane_url_path": "/v1/tunnel"}
        configure = mock.Mock(return_value=self.ready())
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value=""), mock.patch.object(tunnel, "_hidden", side_effect=["", "SYNTHETIC-MCP-ACCESS-KEY"]) as hidden:
            self.assertEqual(0, self.invoke(configure, public=public))
        self.assertEqual(2, hidden.call_count)
        self.assertEqual(public, configure.call_args.args[1])

    def test_pending_returns_prompt_nonzero_without_false_success_or_reboot(self):
        pending = self.ready()
        pending["connection"] = {"alive": True, "verified": False, "state": "connecting"}
        output = io.StringIO()
        self.assertEqual(1, tunnel._wait_managed_connection("/state", pending, output, wait_timeout=0))
        self.assertIn("[PENDING]", output.getvalue())
        self.assertNotIn("[OK]", output.getvalue())
        self.assertNotIn("reboot", output.getvalue().lower())

    def test_failed_service_cannot_use_stale_verified_connection_as_success(self):
        for failure in ({"service_state": "failed", "service_active": False}, {"service_state": "failed", "service_active": True}, {"worker": {"state": "failed"}}):
            with self.subTest(failure=failure):
                state = self.ready()
                state.update(failure)
                output = io.StringIO()
                self.assertEqual(1, tunnel._wait_managed_connection("/state", state, output, wait_timeout=0))
                self.assertIn("[ERROR]", output.getvalue())
                self.assertNotIn("[OK]", output.getvalue())

    def test_readiness_is_polled_and_key_is_requested_only_once(self):
        from framebridge_expanded import autostart
        starting = self.ready()
        starting["connection"] = {"alive": False, "verified": False, "state": "connecting"}
        configure = mock.Mock(return_value=starting)
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="test@example.com"), mock.patch.object(tunnel, "_hidden", side_effect=[ID, "SYNTHETIC-MCP-ACCESS-KEY"]) as hidden, mock.patch.object(tunnel.time, "sleep"), mock.patch.object(autostart, "status", return_value=self.ready()) as status:
            self.assertEqual(0, self.invoke(configure, wait_timeout=2))
        self.assertEqual(2, hidden.call_count)
        self.assertEqual("MCP server access key (hidden): ", hidden.call_args.args[0])
        status.assert_called_once_with("/state")

    def test_interrupt_during_wait_reports_saved_background_connection(self):
        from framebridge_expanded import autostart
        starting = self.ready()
        starting["connection"] = {"alive": False, "verified": False, "state": "connecting"}
        output = io.StringIO()
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="test@example.com"), mock.patch.object(tunnel, "_hidden", side_effect=[ID, "SYNTHETIC-MCP-ACCESS-KEY"]), mock.patch.object(tunnel.time, "sleep"), mock.patch.object(autostart, "status", side_effect=KeyboardInterrupt):
            self.assertEqual(130, self.invoke(mock.Mock(return_value=starting), output=output, wait_timeout=2))
        self.assertIn("continues in the background", output.getvalue())
        self.assertNotIn("Connection cancelled", output.getvalue())

    def test_interrupt_after_commit_does_not_report_connection_cancelled(self):
        marker = {"configured": True, "enabled": True, "interrupted_after_commit": True}
        configure = mock.Mock(return_value=marker)
        output = io.StringIO()
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="test@example.com"), mock.patch.object(tunnel, "_hidden", side_effect=[ID, "SYNTHETIC-MCP-ACCESS-KEY"]):
            self.assertEqual(130, self.invoke(configure, output=output))
        self.assertIn("continues in the background", output.getvalue())
        self.assertNotIn("cancelled", output.getvalue().lower())
        self.assertNotIn("[OK]", output.getvalue())

    def test_resume_status_interrupted_after_commit_is_not_reported_cancelled(self):
        marker = {"configured": True, "enabled": True, "interrupted_after_commit": True}
        output = io.StringIO()
        self.assertEqual(130, tunnel.resume_managed_connection("/state", mock.Mock(return_value=marker), output=output, wait_timeout=0))
        self.assertIn("continues in the background", output.getvalue())
        self.assertNotIn("cancelled", output.getvalue().lower())

    def test_non_tty_fails_before_any_input_or_commit(self):
        configure = mock.Mock()
        with mock.patch.object(tunnel.sys.stdin, "isatty", return_value=False), mock.patch("builtins.input") as label, mock.patch.object(tunnel, "_hidden") as hidden:
            with self.assertRaises(tunnel.TunnelInputError):
                self.invoke(configure)
        label.assert_not_called()
        hidden.assert_not_called()
        configure.assert_not_called()

    def test_resume_uses_no_setup_prompts(self):
        resume = mock.Mock(return_value=self.ready())
        with mock.patch("builtins.input") as label, mock.patch.object(tunnel, "_hidden") as hidden:
            self.assertEqual(0, tunnel.resume_managed_connection("/state", resume, output=io.StringIO(), wait_timeout=0))
        label.assert_not_called()
        hidden.assert_not_called()
        resume.assert_called_once_with("/state")


if __name__ == "__main__":
    unittest.main()
