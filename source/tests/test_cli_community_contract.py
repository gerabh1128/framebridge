"""Command contract tests: mocked interfaces only, no external connections."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from framebridge_expanded import cli
from framebridge_expanded.config import default_config, save_config


class CommunityCliContractTests(unittest.TestCase):
    def call(self, args, modules=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if modules is None:
                code = cli.main(args)
            else:
                with mock.patch.object(cli, "_module", side_effect=lambda name: modules[name]):
                    code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def aggregate(self):
        return {"ok": True, "runtime":{"role":"standalone", "ready":True, "running":True},
                "connection":{"ok":True,"verified":False,"state":"closed"},
                "boot":{"configured":True,"enabled":True,"linger":True,"encrypted_credential_present":True,
                        "service_active":True,"service_state":"active"},
                "desired_connection":"disconnected"}

    def test_short_and_legacy_connect_share_flags(self):
        for args in (["connect", "--replace"], ["connect", "chatgpt", "--replace"]):
            value=cli.build_parser().parse_args(args)
            self.assertTrue(value.replace)
            self.assertFalse(value.foreground)

    def test_common_json_and_profile_options_survive_nested_parsers(self):
        for args in (["--json","--state-dir","/qa/custom","service","boot","disable"],
                     ["service","boot","disable","--json","--state-dir","/qa/custom"]):
            value=cli.build_parser().parse_args(args)
            self.assertTrue(value.json)
            self.assertEqual(value.state_dir,"/qa/custom")
            self.assertEqual(value.boot_action,"disable")

    def test_plain_status_separates_live_connection_and_boot(self):
        life=SimpleNamespace(status=mock.Mock(return_value=self.aggregate()))
        code,text,_=self.call(["status"],{"lifecycle":life})
        self.assertEqual(code,0)
        self.assertIn("Local tools: ready",text)
        self.assertIn("ChatGPT connection: closed",text)
        self.assertIn("Automatic startup: enabled",text)
        self.assertNotIn("connected (verified)",text)

    def test_unavailable_status_is_not_presented_as_stopped_or_disabled(self):
        value={"ok":True,"runtime":{"status_unavailable":True},"connection":{"state":"unavailable"},"boot":{},"desired_connection":"unavailable"}
        life=SimpleNamespace(status=mock.Mock(return_value=value))
        code,text,_=self.call(["status"],{"lifecycle":life})
        self.assertEqual(code,0)
        self.assertIn("Local tools: unavailable",text)
        self.assertIn("Automatic startup: unavailable",text)
        self.assertNotIn("Local tools: stopped",text)

    def test_json_status_is_one_versioned_object(self):
        life=SimpleNamespace(status=mock.Mock(return_value=self.aggregate()))
        code,text,_=self.call(["status","--json"],{"lifecycle":life})
        value=json.loads(text)
        self.assertEqual(code,0)
        self.assertEqual(value["schema_version"],1)
        self.assertEqual(value["version"],"0.3.1")
        self.assertEqual(value["result"]["desired_connection"],"disconnected")
        self.assertFalse(value["result"]["connection"]["verified"])

    def test_scoped_status_uses_same_model_and_includes_intent(self):
        life=SimpleNamespace(status=mock.Mock(return_value=self.aggregate()))
        code,text,_=self.call(["--json","status","connection"],{"lifecycle":life})
        data=json.loads(text)["result"]
        self.assertEqual(code,0)
        self.assertEqual(data["state"],"closed")
        self.assertEqual(data["desired_connection"],"disconnected")
        self.assertNotIn("runtime",data)

    def test_doctor_is_distinct_and_does_not_use_status(self):
        runtime=SimpleNamespace(doctor=mock.Mock(return_value={"ok":False,"compatible_debian_13_amd64":True,
                                "display":"text tools","missing_commands":["tmux"],"issues":[]}))
        code,text,_=self.call(["doctor"],{"runtime":runtime})
        self.assertEqual(code,1)
        self.assertIn("Missing command: tmux",text)
        self.assertIn("use fb status",text)

    def test_json_new_connect_never_prompts_or_starts_services(self):
        with tempfile.TemporaryDirectory() as td, mock.patch("builtins.input") as ask, \
             mock.patch.object(cli,"connect_chatgpt") as connect, mock.patch.object(cli,"_module") as modules:
            code,text,_=self.call(["--state-dir",td,"connect","--json"])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(text)["result"]["error"],"INTERACTION_REQUIRED")
        ask.assert_not_called();connect.assert_not_called();modules.assert_not_called()

    def test_structured_foreground_next_action_preserves_temporary_mode(self):
        with tempfile.TemporaryDirectory() as td,mock.patch.object(cli,"connect_chatgpt") as connect:
            code,text,_=self.call(["--state-dir",td,"connect","--foreground","--json"])
        self.assertEqual(code,2)
        self.assertTrue(json.loads(text)["result"]["next_action"].endswith(" connect --foreground"))
        connect.assert_not_called()

    def test_menu_cancel_returns_cleanly_without_dispatch(self):
        with mock.patch.object(cli.sys.stdin,"isatty",return_value=True),mock.patch("builtins.input",side_effect=KeyboardInterrupt):
            code,text,errors=self.call([])
        self.assertEqual(code,130)
        self.assertIn("Cancelled.",errors)

    def test_json_setup_reports_interaction_without_reading_prompts(self):
        with mock.patch("builtins.input") as ask,mock.patch.object(cli,"setup") as setup:
            code,text,_=self.call(["setup","--json"])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(text)["result"]["error"],"INTERACTION_REQUIRED")
        ask.assert_not_called();setup.assert_not_called()

    def test_saved_json_connect_keeps_human_progress_off_stdout(self):
        life=SimpleNamespace(status=mock.Mock(return_value=self.aggregate()))
        with tempfile.TemporaryDirectory() as td:
            state=Path(td);(state/"autostart").mkdir();(state/"autostart/installation.json").write_text("{}")
            def connect(*args,**kwargs):
                print("Checking the saved connection.")
                return 0
            with mock.patch.object(cli,"connect_chatgpt",side_effect=connect):
                code,text,errors=self.call(["--state-dir",td,"connect","--json"],{"lifecycle":life})
        self.assertEqual(code,0)
        self.assertEqual(json.loads(text)["result"]["runtime"]["ready"],True)
        self.assertIn("Checking the saved connection.",errors)
        self.assertNotIn("Checking",text)

    def test_canonical_disconnect_and_legacy_have_explicit_different_routes(self):
        canonical=SimpleNamespace(disconnect=mock.Mock(return_value={"ok":True}))
        legacy=SimpleNamespace(disconnect=mock.Mock(return_value={"ok":True}))
        modules={"lifecycle":canonical,"connection_background":legacy}
        self.assertEqual(self.call(["disconnect"],modules)[0],0)
        canonical.disconnect.assert_called_once();legacy.disconnect.assert_not_called()
        self.assertEqual(self.call(["disconnect","chatgpt"],modules)[0],0)
        legacy.disconnect.assert_called_once()

    def test_noninteractive_service_start_does_not_invent_authentication_gate(self):
        life=SimpleNamespace(service_start=mock.Mock(return_value={"ok":True,"state":"started","scope":"local_tools"}))
        code,text,_=self.call(["service","start","--json"],{"lifecycle":life})
        self.assertEqual(code,0)
        self.assertEqual(json.loads(text)["result"]["scope"],"local_tools")
        life.service_start.assert_called_once()

    def test_first_node_service_start_reports_interaction_before_provisioning(self):
        life=SimpleNamespace(status=mock.Mock(return_value={"boot":{"configured":False}}),service_start=mock.Mock())
        with tempfile.TemporaryDirectory() as td:
            state=Path(td);config=default_config();config["role"]="node";save_config(state,config)
            code,text,_=self.call(["--state-dir",td,"service","start","--json"],{"lifecycle":life})
        self.assertEqual(code,2)
        self.assertEqual(json.loads(text)["result"]["error"],"INTERACTION_REQUIRED")
        life.service_start.assert_not_called()

    def test_configured_node_service_start_remains_noninteractive(self):
        life=SimpleNamespace(status=mock.Mock(return_value={"boot":{"configured":True}}),service_start=mock.Mock(return_value={"ok":True,"state":"start_requested"}))
        with tempfile.TemporaryDirectory() as td:
            state=Path(td);config=default_config();config["role"]="node";save_config(state,config)
            code,text,_=self.call(["--state-dir",td,"service","start","--json"],{"lifecycle":life})
        self.assertEqual(code,0)
        self.assertEqual(json.loads(text)["result"]["state"],"start_requested")
        life.service_start.assert_called_once()

    def test_boot_disable_and_legacy_autostart_disable_are_not_retargeted(self):
        life=SimpleNamespace(boot_disable=mock.Mock(return_value={"ok":True,"state":"boot-disabled"}))
        auto=SimpleNamespace(disable=mock.Mock(return_value={"ok":True,"state":"disabled"}),
                             status=mock.Mock(return_value={"service_state":"inactive"}))
        modules={"lifecycle":life,"autostart":auto}
        self.call(["service","boot","disable"],modules)
        life.boot_disable.assert_called_once();auto.disable.assert_not_called()
        self.call(["autostart","disable"],modules)
        auto.disable.assert_called_once()

    def test_forget_requires_explicit_confirmation_without_calling_destructive_api(self):
        life=SimpleNamespace(forget_credentials=mock.Mock())
        code,text,_=self.call(["service","credentials","forget","--json"],{"lifecycle":life})
        self.assertEqual(code,2)
        self.assertEqual(json.loads(text)["result"]["error"],"CONFIRMATION_REQUIRED")
        life.forget_credentials.assert_not_called()

    def test_forget_confirmation_instruction_preserves_custom_profile(self):
        life=SimpleNamespace(forget_credentials=mock.Mock())
        code,text,_=self.call(["--state-dir","/qa/profile with spaces","service","credentials","forget","--json"],{"lifecycle":life})
        self.assertEqual(code,2)
        self.assertEqual(json.loads(text)["result"]["next_action"],"fb --state-dir '/qa/profile with spaces' service credentials forget --confirm")
        life.forget_credentials.assert_not_called()

    def test_explicit_confirm_forget_calls_one_narrow_api(self):
        life=SimpleNamespace(forget_credentials=mock.Mock(return_value={"ok":True,"state":"forgotten"}))
        self.assertEqual(self.call(["service","credentials","forget","--confirm","--json"],{"lifecycle":life})[0],0)
        life.forget_credentials.assert_called_once()

    def test_console_login_subcommand_calls_only_login_policy(self):
        startup=SimpleNamespace(status=mock.Mock(return_value={"ok":True,"enabled":False}))
        code,text,_=self.call(["console","login","status","--json"],{"console_startup":startup})
        self.assertEqual(code,0)
        self.assertFalse(json.loads(text)["result"]["enabled"])
        startup.status.assert_called_once()

    def test_terminal_service_group_routes_same_explicit_target(self):
        args=cli.build_parser().parse_args(["tty","service","status","--node","N2"])
        with mock.patch.object(cli,"_target_tty",return_value={"ok":True}) as target,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.tty_command(args,Path("/qa/profile")),0)
        target.assert_called_once_with(Path("/qa/profile"),"N2",service="status")

    def test_noninteractive_typed_text_without_text_never_prompts(self):
        with mock.patch("builtins.input") as ask,mock.patch.object(cli,"tty_command") as tty:
            code,text,_=self.call(["tty","type","a"*32,"--json"])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(text)["result"]["error"],"INTERACTION_REQUIRED")
        ask.assert_not_called();tty.assert_not_called()

    def test_help_uses_same_versioned_guide_without_runtime(self):
        guide=SimpleNamespace(get_guide=mock.Mock(return_value={"ok":True,"topic":"commands","text":"Command guide"}))
        code,text,_=self.call(["help","commands","--json"],{"guide":guide})
        self.assertEqual(code,0)
        self.assertEqual(json.loads(text)["result"]["topic"],"commands")
        guide.get_guide.assert_called_once_with("commands")

    def test_failed_pause_does_not_claim_success(self):
        life=SimpleNamespace(disconnect=mock.Mock(return_value={"ok":False,"error":"TEST_FAILURE"}))
        code,text,errors=self.call(["disconnect"],{"lifecycle":life})
        self.assertEqual(code,1)
        self.assertNotIn("paused until",text)
        self.assertIn("TEST_FAILURE",errors)


if __name__=="__main__":
    unittest.main()
