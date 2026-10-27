import unittest
from pathlib import Path
from unittest.mock import patch
from framebridge_expanded.real_screen import screen_rpc as rpc

class X11InputRoutingTests(unittest.TestCase):
    def test_x11_input_is_rejected_with_graphical_cua_instruction(self):
        for op, value in (("text", {"text": "test"}), ("key", {"key": "ENTER"})):
            request = {"op": op, "capture_id": "a" * 32, **value}
            with self.subTest(op=op), patch.object(rpc, "load_capture", return_value={"screen_type": "x11"}), patch.object(rpc, "act") as hidden_action:
                with self.assertRaisesRegex(rpc.ScreenRpcError, "USE_GRAPHICAL_CUA_FOR_X11_INPUT"):
                    rpc.dispatch(request, Path("/unused/fixture"))
                hidden_action.assert_not_called()

if __name__ == "__main__":
    unittest.main()
