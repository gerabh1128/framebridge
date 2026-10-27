"""Exercise the actual portable CUA routing function without importing its MCP server."""
import ast
import copy
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

def actual_cua(screen, dispatch):
    source = ROOT / "portable/front_door_mcp.py"
    tree = ast.parse(source.read_text())
    function = copy.deepcopy(next(node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "framebridge_cua"))
    function.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"_real_screen_client": screen, "dispatch_cua_request": dispatch}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["framebridge_cua"]

class PortableCaptureScopeTests(unittest.TestCase):
    def test_node_scope_preserves_alias_case_and_uses_real_screen_client(self):
        screen = mock.Mock()
        screen.call.return_value = {"fixture": "actual-client-route-only"}
        dispatch = mock.Mock(side_effect=AssertionError("legacy CUA is not the physical node route"))
        cua = actual_cua(screen, dispatch)
        value = cua("observe", True, capture_scope="  NoDe:CaseSensitive-Node  ", target="UNRELATED")
        self.assertEqual(value, screen.call.return_value)
        screen.call.assert_called_once_with("observe", "CaseSensitive-Node", True)
        dispatch.assert_not_called()

    def test_node_scope_input_is_rejected_without_hidden_or_graphical_fallback(self):
        for action in ["TYPE", "KEY", "CLICK", "DRAG"]:
            with self.subTest(action=action):
                screen, dispatch = mock.Mock(), mock.Mock()
                cua = actual_cua(screen, dispatch)
                value = cua(action, True, capture_scope="node:CaseSensitive-Node", text="not executed")
                self.assertEqual(value["error"], "NODE_SCREEN_SCOPE_OBSERVE_ONLY")
                screen.call.assert_not_called()
                dispatch.assert_not_called()

    def test_authorization_precedes_physical_capture_dispatch(self):
        screen, dispatch = mock.Mock(), mock.Mock()
        cua = actual_cua(screen, dispatch)
        value = cua("OBSERVE", False, capture_scope="node:CaseSensitive-Node")
        self.assertEqual(value["error"], "AUTHORIZED_TRUE_REQUIRED")
        screen.call.assert_not_called()
        dispatch.assert_not_called()

if __name__ == "__main__":
    unittest.main()
