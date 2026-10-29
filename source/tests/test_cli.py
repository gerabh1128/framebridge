import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import cli
from framebridge_expanded.config import default_config, load_config, save_config

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def directory(self):
        return tempfile.TemporaryDirectory(prefix="cli-test-", dir=ROOT / "qa")

    def answers(self, role="standalone", confirm="yes"):
        def ask(prompt):
            if prompt.startswith("Role "):
                return role
            if prompt.startswith("Computer name"):
                return "expanded-test"
            if prompt.startswith("ChatGPT email"):
                return "tester@example.com"
            if prompt.startswith("TLS certificate setup"):
                return "existing"
            if prompt.startswith("Central broker URL"):
                return "https://central.example"
            if prompt.startswith("Save this configuration?"):
                return confirm
            return ""
        return ask

    def test_help_has_english_commands_and_never_loads_runtime(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(cli, "_module") as modules:
            with self.assertRaises(SystemExit) as exited:
                cli.main(["--help"])
        self.assertEqual(exited.exception.code, 0)
        self.assertIn("setup", output.getvalue())
        self.assertIn("connect", output.getvalue())
        modules.assert_not_called()

    def test_no_argument_menu_uses_real_newlines(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="0"):
            self.assertEqual(cli.main([]), 0)
        self.assertIn("FrameBridge\n1. Connect\n2. Status\n3. Doctor", output.getvalue())
        self.assertNotIn("\\n", output.getvalue())

    def test_noninteractive_no_arguments_is_read_only_help(self):
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False), mock.patch.object(cli, "_module") as modules, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([]), 0)
        modules.assert_not_called()

    def test_state_environment_priority_and_alias(self):
        with mock.patch.dict(os.environ, {"FRAMEBRIDGE_EXPANDED_STATE": "/tmp/canonical", "FRAMEBRIDGE_STATE_DIR": "/tmp/alias"}):
            self.assertEqual(cli.resolve_state(), Path("/tmp/canonical"))
            self.assertEqual(cli.resolve_state("/tmp/explicit"), Path("/tmp/explicit"))
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["FRAMEBRIDGE_STATE_DIR"] = "/tmp/alias"
            self.assertEqual(cli.resolve_state(), Path("/tmp/alias"))

    def test_setup_roles_save_public_settings_without_start(self):
        for role in ("standalone", "central", "node"):
            with self.subTest(role=role), self.directory() as td, contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "_module") as modules:
                state = Path(td) / "state"
                self.assertEqual(cli.setup(state, ask=self.answers(role)), 0)
                config = load_config(state)
                self.assertEqual(config["role"], role)
                self.assertEqual(config["account_label"], "tester@example.com")
                self.assertEqual(config["ports"]["mcp"], 20865)
                self.assertEqual((state / "config.json").stat().st_mode & 0o777, 0o600)
                modules.assert_not_called()
                serialized = (state / "config.json").read_text()
                self.assertNotIn("api_key", serialized)
                self.assertNotIn("runtime_key", serialized)

    def test_setup_new_profile_selects_auto_without_a_desktop_question(self):
        prompts = []
        answer = self.answers()
        def ask(prompt):
            prompts.append(prompt)
            return answer(prompt)
        with self.directory() as td, contextlib.redirect_stdout(io.StringIO()) as output:
            state = Path(td) / "state"
            self.assertEqual(cli.setup(state, ask=ask), 0)
            self.assertEqual(load_config(state)["headless"], "auto")
        self.assertFalse(any("desktop" in prompt.lower() for prompt in prompts))
        self.assertIn("Desktop: automatic detection.", output.getvalue())

    def test_setup_keeps_existing_explicit_desktop_setting(self):
        for mode in ("yes", "no", True, False):
            with self.subTest(mode=mode), self.directory() as td, contextlib.redirect_stdout(io.StringIO()) as output:
                state = Path(td) / "state"
                settings = default_config(); settings["headless"] = mode
                save_config(state, settings)
                runtime = mock.Mock(); runtime.status.return_value = {"running": False, "processes": []}
                with mock.patch.object(cli, "_module", return_value=runtime):
                    self.assertEqual(cli.setup(state, ask=self.answers()), 0)
                self.assertEqual(load_config(state)["headless"], mode)
                self.assertIn("setting preserved.", output.getvalue())
                self.assertNotIn("Desktop: automatic detection.", output.getvalue())

    def autostart_enable_output(self, result):
        autostart = mock.Mock(); autostart.enable.return_value = result
        with mock.patch.object(cli, "_module", return_value=autostart), contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(["autostart", "enable"])
        return code, output.getvalue()

    def test_autostart_enable_distinguishes_configured_from_connected(self):
        result = {"configured": True, "enabled": True, "service_state": "activating",
                  "runtime": {"ready": False, "display_context": {"kind": "text"}},
                  "connection": {"state": "checking", "verified": False}}
        code, output = self.autostart_enable_output(result)
        self.assertEqual(code, 0)
        self.assertIn("Automatic startup enabled.", output)
        self.assertIn("Tools: starting.", output)
        self.assertIn("Desktop: text mode.", output)
        self.assertIn("Connection: checking in the background.", output)
        self.assertNotIn("Connection: ready.", output)
        self.assertNotIn("CUA ready", output)
        self.assertNotIn('"configured"', output)

    def test_autostart_enable_reports_verified_graphical_capabilities(self):
        result = {"configured": True, "enabled": True, "service_state": "active",
                  "runtime": {"ready": True, "display_context": {"kind": "physical-x11"},
                              "capabilities": {"fbexec": True, "fbterm": True, "cua_tty_module": True, "graphical_cua": True}},
                  "connection": {"state": "ready", "verified": True}}
        code, output = self.autostart_enable_output(result)
        self.assertEqual(code, 0)
        self.assertIn("Tools: FBEXEC, FBTERM, CUA-TTY ready.", output)
        self.assertIn("Desktop: detected; CUA ready.", output)
        self.assertIn("Connection: ready.", output)

    def test_autostart_enable_failed_service_is_not_a_success_exit(self):
        code, output = self.autostart_enable_output({"configured": True, "enabled": True,
            "service_state": "failed", "runtime": {"status_unavailable": True},
            "connection": {"state": "error", "verified": False}})
        self.assertEqual(code, 1)
        self.assertIn("Service: failed", output)
        self.assertIn("Tools: status unavailable.", output)
        self.assertIn("Connection: needs attention.", output)
        self.assertNotIn("CUA ready", output)

    def test_autostart_enable_cancel_has_no_success_summary(self):
        code, output = self.autostart_enable_output({"state": "cancelled"})
        self.assertEqual(code, 130)
        self.assertEqual(output, "Automatic startup was not enabled.\n")

    def test_setup_decline_does_not_create_profile(self):
        with self.directory() as td, contextlib.redirect_stdout(io.StringIO()):
            state = Path(td) / "state"
            self.assertEqual(cli.setup(state, ask=self.answers(confirm="no")), 0)
            self.assertFalse((state / "config.json").exists())

    def test_setup_rejects_running_profile_before_prompts(self):
        with self.directory() as td:
            state = Path(td)
            save_config(state, default_config(name="existing"))
            before = (state / "config.json").read_bytes()
            runtime = mock.Mock()
            runtime.status.return_value = {"running": True, "processes": []}
            ask = mock.Mock()
            with mock.patch.object(cli, "_module", return_value=runtime):
                with self.assertRaisesRegex(RuntimeError, "Stop this profile"):
                    cli.setup(state, ask=ask)
            ask.assert_not_called()
            self.assertEqual((state / "config.json").read_bytes(), before)

    def connection_runtime(self, base):
        runtime = mock.Mock()
        runtime.PREFIX = Path(base) / "payload"
        binary = runtime.PREFIX / "original/geranas/FrameBridge-MCP-Private/bin/tunnel-client"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"synthetic test binary")
        runtime.status.return_value = {"running": False, "ready": False}
        return runtime

    def test_connection_rejects_another_profiles_listening_port(self):
        with self.directory() as td:
            state = Path(td) / "profile"
            save_config(state, default_config(name="stopped-profile"))
            before = (state / "config.json").read_bytes()
            runtime = self.connection_runtime(td)
            automatic = mock.Mock()
            listener = mock.MagicMock()
            listener.__enter__.return_value.bind.side_effect = OSError("address in use")
            def connect(*args, **kwargs):
                return kwargs["configure_connection"](state, {"account_label": "test@example.com", "tunnel_id": "tunnel_new"}, "SYNTHETIC-MCP-ACCESS-KEY")
            with mock.patch.object(cli, "_module", side_effect=lambda name: runtime if name == "runtime" else automatic), mock.patch.object(cli.socket, "socket", return_value=listener), mock.patch.object(cli.tunnel, "connect_chatgpt", side_effect=connect):
                with self.assertRaisesRegex(RuntimeError, "MCP port 20865 is already in use"):
                    cli.connect_chatgpt(state)
            automatic.configure_connection.assert_not_called()
            self.assertEqual(before, (state / "config.json").read_bytes())

    def test_new_connection_needs_no_prior_profile_or_runtime_start(self):
        with self.directory() as td:
            state = Path(td) / "new-profile"
            runtime = self.connection_runtime(td)
            with mock.patch.object(cli, "_module", return_value=runtime), mock.patch.object(cli.tunnel, "connect_chatgpt", return_value=130) as connect:
                self.assertEqual(130, cli.connect_chatgpt(state))
            self.assertFalse(state.exists())
            runtime.start.assert_not_called()
            runtime.status.assert_not_called()
            public = connect.call_args.args[3]
            self.assertEqual("standalone", public["role"])
            self.assertEqual("auto", public["headless"])
            self.assertTrue(callable(connect.call_args.kwargs["configure_connection"]))

    def test_cancel_new_profile_at_hidden_key_leaves_no_profile_or_services(self):
        with self.directory() as td:
            state = Path(td) / "new-profile"
            runtime = self.connection_runtime(td)
            automatic = mock.Mock()
            with mock.patch.object(cli, "_module", side_effect=lambda name: runtime if name == "runtime" else automatic), mock.patch.object(cli.tunnel.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="test@example.com"), mock.patch.object(cli.tunnel, "_hidden", side_effect=["tunnel_valid", KeyboardInterrupt]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(130, cli.connect_chatgpt(state))
            self.assertFalse(state.exists())
            automatic.configure_connection.assert_not_called()
            runtime.start.assert_not_called()

    def test_existing_managed_connection_dispatches_resume_without_wizard(self):
        with self.directory() as td:
            state = Path(td) / "profile"
            save_config(state, default_config())
            (state / "autostart").mkdir()
            (state / "autostart/installation.json").write_text("{}")
            runtime = self.connection_runtime(td)
            automatic = mock.Mock()
            with mock.patch.object(cli, "_module", side_effect=lambda name: runtime if name == "runtime" else automatic), mock.patch.object(cli.tunnel, "resume_managed_connection", return_value=0) as resume, mock.patch.object(cli.tunnel, "connect_chatgpt") as wizard:
                self.assertEqual(0, cli.connect_chatgpt(state))
            resume.assert_called_once_with(state, automatic.resume_connection)
            wizard.assert_not_called()

    def test_replace_bypasses_resume_and_uses_normal_three_field_wizard(self):
        with self.directory() as td:
            state = Path(td) / "profile"
            save_config(state, default_config())
            (state / "autostart").mkdir()
            (state / "autostart/installation.json").write_text("{}")
            runtime = self.connection_runtime(td)
            with mock.patch.object(cli, "_module", return_value=runtime), mock.patch.object(cli.tunnel, "resume_managed_connection") as resume, mock.patch.object(cli.tunnel, "connect_chatgpt", return_value=0) as wizard:
                self.assertEqual(0, cli.connect_chatgpt(state, replace=True))
            resume.assert_not_called()
            self.assertTrue(callable(wizard.call_args.kwargs["configure_connection"]))

    def test_replace_dispatch_and_foreground_are_mutually_exclusive(self):
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "connect_chatgpt", return_value=0) as connect:
            self.assertEqual(0, cli.main(["--state-dir", "/tmp/profile", "connect", "chatgpt", "--replace"]))
        connect.assert_called_once_with(Path("/tmp/profile"), foreground=False, replace=True)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.main(["connect", "chatgpt", "--replace", "--foreground"])
        self.assertEqual(2, error.exception.code)

    def test_node_profile_cannot_tunnel_a_central_profiles_port(self):
        with self.directory() as td:
            state = Path(td)
            save_config(state, default_config("node", "node-profile"))
            with mock.patch.object(cli, "_module") as modules, mock.patch.object(cli.tunnel, "connect_chatgpt") as connect:
                with self.assertRaisesRegex(RuntimeError, "central profile"):
                    cli.connect_chatgpt(state)
            modules.assert_not_called()
            connect.assert_not_called()

    def test_lifecycle_dispatch_receives_explicit_profile(self):
        runtime = mock.Mock()
        runtime.status.return_value = {"ok": True, "running": False}
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "_module", return_value=runtime):
            self.assertEqual(cli.main(["--state-dir", str(ROOT / "qa/example"), "status"]), 0)
        runtime.status.assert_called_once_with(ROOT / "qa/example")

    def test_tty_dispatch_uses_authorized_typed_payload(self):
        tty = mock.Mock()
        tty.request.return_value = {"ok": True}
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "_module", return_value=tty):
            self.assertEqual(cli.main(["--state-dir", str(ROOT / "qa/example"), "tty", "key", "session-1", "Enter"]), 0)
        tty.request.assert_called_once_with(ROOT / "qa/example", {"authorized": True, "op": "key.send", "session_id": "session-1", "key": "Enter"})

    def test_node_enrollment_does_not_start_agent(self):
        with self.directory() as td:
            state = Path(td)
            config = default_config("node", "node-test")
            config["broker_url"] = "https://central.example"
            save_config(state, config)
            node = mock.Mock()
            node.enroll.return_value = {"ok": True, "state": "pending"}
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "_module", return_value=node):
                self.assertEqual(cli.main(["--state-dir", str(state), "nodes", "enroll"]), 0)
            node.enroll.assert_called_once_with("https://central.example", "node-test", state_dir=state)
            node.run.assert_not_called()

    def test_nodes_list_scopes_and_restores_environment(self):
        with self.directory() as td:
            state = Path(td)
            save_config(state, default_config(name="list-test"))
            broker = mock.Mock()
            fleet = mock.Mock()
            def discover():
                self.assertEqual(os.environ["FRAMEBRIDGE_EXPANDED_STATE"], str(state))
                return {"ok": True, "nodes": []}
            fleet.discover_nodes.side_effect = discover
            before = os.environ.get("FRAMEBRIDGE_EXPANDED_STATE")
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "_module", side_effect=lambda name: broker if name == "broker" else fleet):
                self.assertEqual(cli.main(["--state-dir", str(state), "nodes", "list"]), 0)
            broker.configure.assert_called_once_with(None, state_dir=state)
            self.assertEqual(os.environ.get("FRAMEBRIDGE_EXPANDED_STATE"), before)

    def test_nodes_approval_requires_expected_uuid(self):
        parser = cli.build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["nodes", "approve", "request-1"])


    def test_central_wizard_creates_lan_tls_only_after_review(self):
        with self.directory() as td, contextlib.redirect_stdout(io.StringIO()) as output:
            state = Path(td) / "state"
            original = self.answers("central")
            def ask(prompt):
                if prompt.startswith("Broker listen host"):
                    return "0.0.0.0"
                if prompt.startswith("TLS certificate setup"):
                    return "generate"
                if prompt.startswith("Certificate DNS name"):
                    return "central.example.test"
                if prompt.startswith("Advertised broker IP address"):
                    return "192.0.2.20"
                return original(prompt)
            self.assertEqual(cli.setup(state, ask=ask), 0)
            config = load_config(state)
            self.assertEqual(config["broker_url"], "https://192.0.2.20:20878")
            self.assertEqual(config["broker_control_url"], "https://127.0.0.1:20878")
            self.assertEqual(config["broker_listen_host"], "0.0.0.0")
            self.assertTrue(Path(config["tls_ca"]).is_file())
            self.assertFalse((state / "manager.json").exists())
            self.assertIn("No service or ChatGPT connection was started.", output.getvalue())
            self.assertNotIn("PRIVATE KEY", output.getvalue())

    def test_certificates_create_dispatch_uses_explicit_profile(self):
        with self.directory() as td, contextlib.redirect_stdout(io.StringIO()) as output:
            state = Path(td)
            save_config(state, default_config("central", "cert-cli"))
            self.assertEqual(cli.main(["--state-dir", str(state), "certificates", "create",
                                       "--host", "cert.example.test", "--address", "192.0.2.30",
                                       "--listen-host", "0.0.0.0"]), 0)
            self.assertEqual(load_config(state)["broker_url"], "https://192.0.2.30:20878")
            self.assertIn("ca_fingerprint_sha256", output.getvalue())
            self.assertNotIn("PRIVATE KEY", output.getvalue())


if __name__ == "__main__":
    unittest.main()
