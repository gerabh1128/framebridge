"""Regression coverage for route and state across the TTY and screen tools."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "expanded"))

from framebridge_expanded.real_screen.screen_frontdoor import ScreenClient
from framebridge_expanded.tty_mcp import _remote_request, register_tools


NODE_ID = "731320ca-b3a5-4ae7-9598-2746adbc2162"
ALIAS = "gerameg-lab"
HOST = "GERAMEG"
CAPTURE = "a" * 32
NOT_STARTED = ("not_started", "expired_before_dispatch", "expired_before_start")


def receipt(call, *, state="completed", request_id="broker-receipt"):
    return {
        "requested_target": call["target"], "target": ALIAS, "resolved_target": ALIAS,
        "node": ALIAS, "node_id": NODE_ID, "registered_label": ALIAS,
        "hostname": HOST if state == "completed" else None,
        "executed_hostname": HOST if state == "completed" else None,
        "receiver_hostname": "GERANAS", "request_id": request_id,
        "mcp_job_id": call["job_id"], "mcp_script_sha256": call["sha256"],
        "execution_state": state,
    }


def failure(call, state):
    return {"ok": False, "error": "BROKER_" + state.upper(), **receipt(call, state=state)}


def wire(value, *, count=1):
    raw = json.dumps(value).encode("utf-8")
    packed = base64.b64encode(zlib.compress(raw)).decode("ascii")
    part_length = (len(packed) + count - 1) // count
    return [{"ok": True, "ticket": "a" * 32, "wire": "zlib-base64-v1",
             "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
             "chunks": count, "index": i,
             "data": packed[i * part_length:(i + 1) * part_length]}
            for i in range(count)]


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class TtyReceiptTests(unittest.TestCase):
    def test_remote_tty_preserves_requested_uuid_and_resolved_alias_after_timeout(self):
        for state in (*NOT_STARTED, "in_flight", "outcome_unknown"):
            with self.subTest(state=state):
                mcp = Registry()
                register_tools(mcp, lambda **kwargs: failure(kwargs, state), screen_client=object())
                out = mcp.tools["cua_tty_open"](authorized=True, target=NODE_ID)
                self.assertEqual(out["requested_target"], NODE_ID)
                self.assertEqual(out["target"], ALIAS)
                self.assertEqual(out["node_id"], NODE_ID)
                self.assertIsNone(out["executed_hostname"])
                self.assertEqual(out["execution_state"], state)
                self.assertEqual(out["request_id"], "broker-receipt")
                self.assertEqual(out.get("do_not_replay", False), state not in NOT_STARTED)

    def test_remote_tty_authenticates_chunks_by_uuid_and_retains_original_receipt(self):
        parts = wire({"ok": True, "result": {"session_id": "tty"}, "node": "FORGED",
                      "executed_hostname": "FORGED", "request_id": "forged"}, count=2)
        calls = []

        def execute(**kwargs):
            i = len(calls)
            calls.append(kwargs["target"])
            return {"ok": True, "stdout": json.dumps(parts[i]),
                    **receipt(kwargs, request_id=f"receipt-{i}")}

        out = _remote_request(execute, ALIAS, {"op": "open"})
        self.assertEqual(calls, [ALIAS, NODE_ID])
        self.assertEqual(out["result"], {"session_id": "tty"})
        self.assertEqual(out["target"], ALIAS)
        self.assertEqual(out["node"], ALIAS)
        self.assertEqual(out["executed_hostname"], HOST)
        self.assertEqual(out["request_id"], "receipt-0")

    def test_delivery_timeout_does_not_turn_completed_tty_action_into_expired_action(self):
        first, _ = wire({"ok": True}, count=2)

        def execute(**kwargs):
            if kwargs["target"] == ALIAS:
                return {"ok": True, "stdout": json.dumps(first), **receipt(kwargs)}
            return failure(kwargs, "expired_before_dispatch")

        out = _remote_request(execute, ALIAS, {"op": "open"})
        self.assertEqual(out["error"], "TTY_REMOTE_RESULT_UNAVAILABLE")
        self.assertEqual(out["execution_state"], "completed")
        self.assertTrue(out["do_not_replay"])
        self.assertEqual(out["result_delivery"]["execution_state"], "expired_before_dispatch")


class ScreenReceiptTests(unittest.TestCase):
    def call_key(self, execute):
        client = ScreenClient(execute, lambda **kwargs: kwargs, resolve_node=lambda _: ALIAS)
        return client.call("key", None, True, target=NODE_ID, capture_id=CAPTURE, key="ENTER")

    def test_screen_distinguishes_queue_expiry_from_uncertain_action(self):
        for state in (*NOT_STARTED, "in_flight", "outcome_unknown"):
            with self.subTest(state=state):
                out = self.call_key(lambda **kwargs: failure(kwargs, state))
                self.assertEqual(out["execution_state"], state)
                self.assertEqual(out["request_id"], "broker-receipt")
                self.assertEqual(out["requested_target"], NODE_ID)
                self.assertEqual(out["resolved_target"], ALIAS)
                self.assertEqual(out["node_id"], NODE_ID)
                if state in NOT_STARTED:
                    self.assertIs(out["performed"], False)
                    self.assertNotIn("observe_again", out)
                    self.assertFalse(out.get("do_not_replay", False))
                else:
                    self.assertNotIn("performed", out)
                    self.assertTrue(out["do_not_replay"])
                    self.assertTrue(out["observe_again"])

    def test_mismatched_receipt_cannot_prove_screen_action_did_not_start(self):
        def execute(**kwargs):
            return {**failure(kwargs, "expired_before_dispatch"), "mcp_job_id": "different"}

        out = self.call_key(execute)
        self.assertEqual(out["error"], "SCREEN_EXEC_BINDING_INVALID")
        self.assertNotIn("performed", out)
        self.assertTrue(out["do_not_replay"])

    def test_screen_chunk_expiry_preserves_confirmed_input_and_separate_delivery_state(self):
        calls = []

        def execute(**kwargs):
            calls.append(kwargs)
            if len(calls) > 1:
                return failure(kwargs, "expired_before_dispatch")
            manifest = {"capture_id": "b" * 32, "node": HOST, "bytes": 100, "width": 5,
                        "height": 5, "sha256": "0" * 64, "source": "linux-framebuffer",
                        "authentic_pixels": True, "full_screen": True,
                        "action": {"kind": "key", "previous_capture_id": CAPTURE,
                                   "result": {"performed": True}}}
            return {"ok": True, "exit_code": "0",
                    "stdout": json.dumps({"ok": True, "result": manifest}), **receipt(kwargs)}

        out = self.call_key(execute)
        self.assertEqual(len(calls), 2)
        self.assertTrue(out["performed"])
        self.assertTrue(out["do_not_replay"])
        self.assertEqual(out["execution_state"], "completed")
        self.assertEqual(out["mcp_job_id"], calls[0]["job_id"])
        self.assertEqual(out["result_delivery"]["execution_state"], "expired_before_dispatch")
        self.assertEqual(out["result_delivery"]["mcp_job_id"], calls[1]["job_id"])


if __name__ == "__main__":
    unittest.main()
