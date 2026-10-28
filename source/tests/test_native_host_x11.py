"""Native-host TYPE interception tests with an entirely fake CUA/display.

No captured code, browser, display, library loader, or native input is used.
"""
import importlib.util
import ast
import ctypes as C
from pathlib import Path
import sys
import subprocess
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock


PACKAGE = "_framebridge_native_host_x11_test"
ROOT = Path(__file__).parent
if not (ROOT / "x11_text.py").is_file():
    ROOT = ROOT.parent / "expanded/framebridge_expanded"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
display = ModuleType(PACKAGE + ".display_context")


class DisplayContextError(RuntimeError):
    pass


display.DisplayContextError = DisplayContextError
display.check = lambda env: None
display.describe = lambda env: {"identity": "fixture-owner"}
sys.modules[PACKAGE] = package
sys.modules[display.__name__] = display
text_spec = importlib.util.spec_from_file_location(PACKAGE + ".x11_text", ROOT / "x11_text.py")
text_module = importlib.util.module_from_spec(text_spec)
sys.modules[text_spec.name] = text_module
text_spec.loader.exec_module(text_module)
spec = importlib.util.spec_from_file_location(PACKAGE + ".native_host", ROOT / "native_host.py")
native_host = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = native_host
spec.loader.exec_module(native_host)


class CuaTransportError(RuntimeError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FakeCua:
    CuaTransportError = CuaTransportError

    def __init__(self, *, with_base=False):
        self.calls = []
        self.context_calls = []
        self.context = {"before_id": 101, "width": 1920, "height": 1080}
        self.argv = None
        self.mutate_context_after_capture = False
        self.token = object()
        if with_base:
            self._run_v3_base = self._primitive

    def _primitive(self, argv, timeout=15):
        self.calls.append((list(argv), timeout))
        return self.token

    def _run(self, argv, timeout=15):
        if hasattr(self, "_run_v3_base"):
            return self._run_v3_base(argv, timeout=timeout)
        return self._primitive(argv, timeout=timeout)

    def _action_context(self, message, *args, **kwargs):
        self.context_calls.append((message, args, kwargs))
        return self.context

    def handle_cua_message(self, message):
        self._action_context(message)
        if self.mutate_context_after_capture:
            self.context.update(before_id=999, width=800, height=600)
        argv = self.argv
        if argv is None:
            if message.get("action") == "TYPE":
                argv = ["xdotool", "type", "--clearmodifiers", "--delay", "10", "--", message["text"]]
            else:
                argv = ["xdotool", "getactivewindow"]
        return self._run(argv, timeout=7)


class NativeHostX11Tests(unittest.TestCase):
    def install(self, cua=None, sender=None, checker=None):
        cua = cua or FakeCua()
        sender = sender or Mock(return_value=None)
        checker = checker or Mock()
        state = native_host.install_cua_guard(cua, env={"DISPLAY": ":fixture"},
                                               checker=checker, text_sender=sender)
        return cua, sender, checker, state

    def test_exact_type_intercept_carries_original_context_and_timeout(self):
        cua, sender, _, state = self.install()
        result = cua.handle_cua_message({"action": "TYPE", "text": "-S /run/test"})
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.args, ["xdotool", "type", "--clearmodifiers", "--delay", "10", "--", "-S /run/test"])
        self.assertEqual(cua.calls, [])
        self.assertEqual(len(cua.context_calls), 1)
        args, kwargs = sender.call_args
        self.assertEqual(args, ("-S /run/test",))
        self.assertEqual(kwargs["expected_window"], 101)
        self.assertEqual(kwargs["expected_geometry"], (1920, 1080))
        self.assertEqual(kwargs["timeout"], 7)
        self.assertTrue(callable(kwargs["before_input"]))
        self.assertFalse(state["input_started"])

    def test_captured_context_is_a_snapshot_not_a_mutable_alias(self):
        cua = FakeCua()
        cua.mutate_context_after_capture = True
        cua, sender, _, _ = self.install(cua)
        cua.handle_cua_message({"action": "TYPE", "text": "/"})
        self.assertEqual(sender.call_args.kwargs["expected_window"], 101)
        self.assertEqual(sender.call_args.kwargs["expected_geometry"], (1920, 1080))

    def test_intercepts_when_captured_runner_has_a_base_runner(self):
        cua, sender, _, _ = self.install(FakeCua(with_base=True))
        cua.handle_cua_message({"action": "TYPE", "text": "/"})
        sender.assert_called_once()
        self.assertEqual(cua.calls, [])

    def test_unexpected_type_arguments_fail_closed(self):
        forms = [
            ["xdotool", "type", "--delay", "10", "--", "/"],
            ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", "/"],
            ["xdotool", "type", "--clearmodifiers", "--delay", "10", "--", "/", "extra"],
            ["xdotool", "type", "--window", "999", "--", "/"],
            ["xdotool", "TYPE", "--clearmodifiers", "--delay", "10", "--", "/"],
        ]
        for argv in forms:
            with self.subTest(argv=argv):
                cua = FakeCua()
                cua.argv = argv
                cua, sender, _, state = self.install(cua)
                with self.assertRaises(CuaTransportError):
                    cua.handle_cua_message({"action": "TYPE", "text": "/"})
                self.assertEqual(cua.calls, [])
                sender.assert_not_called()
                self.assertFalse(state["input_started"])

    def test_missing_original_context_rejects_before_sender_or_runner(self):
        cua = FakeCua()
        cua.context = {}
        cua, sender, _, state = self.install(cua)
        with self.assertRaises(CuaTransportError):
            cua.handle_cua_message({"action": "TYPE", "text": "/"})
        sender.assert_not_called()
        self.assertEqual(cua.calls, [])
        self.assertFalse(state["input_started"])

    def test_non_type_query_passes_through_and_stays_read_only(self):
        cua, sender, _, state = self.install()
        result = cua.handle_cua_message({"action": "QUERY"})
        self.assertIs(result, cua.token)
        self.assertEqual(cua.calls, [(["xdotool", "getactivewindow"], 7)])
        sender.assert_not_called()
        self.assertFalse(state["input_started"])

    def test_non_type_query_with_invalid_action_context_still_passes_through(self):
        cua = FakeCua()
        cua.context = {"unrelated_query_context": True}
        cua, sender, _, state = self.install(cua)
        self.assertIs(cua.handle_cua_message({"action": "QUERY"}), cua.token)
        self.assertEqual(cua.calls, [(["xdotool", "getactivewindow"], 7)])
        sender.assert_not_called()
        self.assertFalse(state["input_started"])

    def test_search_literal_type_is_a_read_only_query(self):
        cua = FakeCua()
        cua.context = {"query_context": True}
        cua.argv = ["xdotool", "search", "--name", "type"]
        cua, sender, _, state = self.install(cua)
        self.assertIs(cua.handle_cua_message({"action": "QUERY"}), cua.token)
        self.assertEqual(cua.calls, [(["xdotool", "search", "--name", "type"], 7)])
        sender.assert_not_called()
        self.assertFalse(state["input_started"])

    def test_clipboard_context_passes_through_without_clipboard_access(self):
        cua = FakeCua()
        cua.context = {"clipboard_context_is_not_type_keys_context": True}
        cua.argv = ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
        cua, sender, _, state = self.install(cua)
        result = cua.handle_cua_message({"action": "TYPE", "type_mode": "CLIPBOARD_EXACT", "text": "fixture"})
        self.assertIs(result, cua.token)
        self.assertEqual(cua.calls, [(["xdotool", "key", "--clearmodifiers", "ctrl+v"], 7)])
        sender.assert_not_called()
        self.assertTrue(state["input_started"])

    def test_actual_frozen_type_consumes_completed_process_contract(self):
        candidates = [Path(__file__).with_name("frozen_type_fixture.py")]
        for parent in [ROOT, *ROOT.parents]:
            candidates.append(parent / "sources/original-asus/framebridge-native/host/framebridge_cua_linux.py")
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            self.skipTest("captured original _type source is not available in this checkout")
        tree = ast.parse(source.read_text())
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_type"]
        self.assertTrue(definitions, str(source))
        cua = FakeCua()
        namespace = {"CuaTransportError": CuaTransportError,
                     "_run": lambda argv, timeout: cua._run(argv, timeout=timeout),
                     "_action_context": lambda message: cua._action_context(message),
                     "_evidence_lines": lambda context: []}
        exec(compile(ast.Module(body=[definitions[-1]], type_ignores=[]), str(source), "exec"), namespace)
        cua.handle_cua_message = namespace["_type"]
        cua, sender, _, state = self.install(cua)
        result = cua.handle_cua_message({"action": "TYPE", "text": "-S /run/fixture"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "TYPE")
        self.assertIn("RESULTADO_FINAL=CUA_TYPE_PASS", result["_frame_output"])
        self.assertEqual(sender.call_args.kwargs["timeout"], 20)
        self.assertEqual(cua.calls, [])

    def test_non_type_input_keeps_existing_input_tracking(self):
        cua = FakeCua()
        cua.argv = ["xdotool", "key", "Return"]
        cua, sender, _, state = self.install(cua)
        self.assertIs(cua.handle_cua_message({"action": "KEY"}), cua.token)
        self.assertEqual(cua.calls, [(["xdotool", "key", "Return"], 7)])
        sender.assert_not_called()
        self.assertTrue(state["input_started"])

    def test_sender_preflight_failure_reports_no_input(self):
        # Use the same error class the native sender exports, without loading
        # or instantiating its native backend.
        sender = Mock(side_effect=text_module.TextError("TYPE_TEXT_UNREPRESENTABLE_IN_CURRENT_GROUP"))
        cua, _, _, state = self.install(sender=sender)
        with self.assertRaises(CuaTransportError):
            cua.handle_cua_message({"action": "TYPE", "text": "\u2603"})
        self.assertFalse(state["input_started"])
        self.assertEqual(cua.calls, [])
        response = native_host.decorate_response({"executed": False}, state, {})
        self.assertFalse(response["input_may_have_been_sent"])
        self.assertFalse(response["do_not_replay"])
        self.assertFalse(response["performed"])

    def test_input_started_only_when_sender_calls_guard(self):
        observed = []
        holder = {}

        def sender(text, **kwargs):
            observed.append(holder["state"]["input_started"])
            kwargs["before_input"]()
            observed.append(holder["state"]["input_started"])

        cua, _, _, state = self.install(sender=sender)
        holder["state"] = state
        cua.handle_cua_message({"action": "TYPE", "text": "a"})
        self.assertEqual(observed, [False, True])
        self.assertEqual(cua.calls, [])

    def test_authority_failure_inside_sender_guard_sends_no_input(self):
        gate = {"sender_active": False}

        def checker(env):
            if gate["sender_active"]:
                raise DisplayContextError("IDENTITY_CHANGED")

        def sender(text, **kwargs):
            gate["sender_active"] = True
            kwargs["before_input"]()
        cua, _, _, state = self.install(sender=sender, checker=checker)
        with self.assertRaises(CuaTransportError):
            cua.handle_cua_message({"action": "TYPE", "text": "/"})
        self.assertFalse(state["input_started"])
        self.assertEqual(cua.calls, [])

    def test_post_input_failure_has_do_not_replay_and_no_performed_false(self):
        # The injected sender uses the module instance imported by native_host.
        def sender(text, **kwargs):
            kwargs["before_input"]()
            raise text_module.TextError("TYPE_KEYMAP_CHANGED")

        cua, _, _, state = self.install(sender=sender)
        with self.assertRaises(CuaTransportError):
            cua.handle_cua_message({"action": "TYPE", "text": "/"})
        response = native_host.decorate_response({"executed": False}, state, {})
        self.assertTrue(response["input_may_have_been_sent"])
        self.assertTrue(response["do_not_replay"])
        self.assertNotIn("performed", response)
        self.assertEqual(cua.calls, [])

    def test_successful_callback_marks_performed_in_existing_response(self):
        sender = Mock(side_effect=lambda text, **kwargs: kwargs["before_input"]())
        cua, _, _, state = self.install(sender=sender)
        cua.handle_cua_message({"action": "TYPE", "text": "/"})
        response = native_host.decorate_response({"executed": True}, state, {})
        self.assertTrue(response["performed"])
        self.assertTrue(response["input_may_have_been_sent"])
        self.assertFalse(response["do_not_replay"])

    def test_new_message_does_not_reuse_previous_action_context(self):
        cua, sender, _, state = self.install()
        cua.handle_cua_message({"action": "TYPE", "text": "first"})
        native_host.decorate_response({"executed": True}, state, {})
        cua.context = {}
        with self.assertRaises(CuaTransportError):
            cua.handle_cua_message({"action": "TYPE", "text": "second"})
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(cua.calls, [])

    def test_native_snapshot_rejects_held_shift_before_any_input(self):
        backend = object.__new__(text_module.NativeBackend)
        backend.display = 1
        backend.context = 1
        backend.modifier_codes = {50}

        def keymap(display_id, buffer):
            data = bytearray(32)
            data[50 // 8] = 1 << (50 % 8)
            C.memmove(buffer, bytes(data), 32)
            return 1

        def active_window(context, result):
            result._obj.value = 101
            return 0

        backend.x11 = SimpleNamespace(XkbGetState=Mock(return_value=0),
                                      XQueryKeymap=keymap, XDefaultScreen=Mock())
        backend.xdo = SimpleNamespace(xdo_get_active_window=active_window)
        backend.xtst = SimpleNamespace(XTestFakeKeyEvent=Mock())
        with self.assertRaisesRegex(text_module.TextError, "^TYPE_HELD_MODIFIERS_UNSUPPORTED$"):
            backend.snapshot()
        backend.xtst.XTestFakeKeyEvent.assert_not_called()
        backend.x11.XDefaultScreen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
