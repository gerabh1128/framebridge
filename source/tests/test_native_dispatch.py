"""Dispatch-race and uncertain-outcome tests using inert CUA engines only."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from framebridge_expanded import display_context as dc
from framebridge_expanded import native_host as nh
from framebridge_expanded import adapter as ad

class CuaError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)

class NativeDispatchTests(unittest.TestCase):
    def fake(self, calls, action=None):
        cua = SimpleNamespace(CuaTransportError=CuaError)
        def run(argv, timeout=15):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        cua._run = run
        cua.handle_cua_message = lambda msg: action(cua) if action else {"ok": True}
        return cua

    def checker(self, fail_at):
        state = {"count": 0}
        def check(env):
            state["count"] += 1
            if state["count"] >= fail_at:
                raise dc.DisplayContextError("CUA_FOREGROUND_SESSION_CHANGED")
            return {}
        return check

    def test_queue_then_context_change_blocks_first_primitive(self):
        calls = []
        cua = self.fake(calls, lambda engine: engine._run(["xdotool", "key", "a"]))
        state = nh.install_cua_guard(cua, env={}, checker=self.checker(2))
        with self.assertRaises(CuaError):
            cua.handle_cua_message({})
        self.assertEqual(calls, [])
        result = nh.decorate_response({"executed": False}, state, {})
        self.assertFalse(result["input_may_have_been_sent"])
        self.assertFalse(result["performed"])
        self.assertFalse(result["do_not_replay"])

    def test_context_change_between_query_and_input_blocks_input(self):
        calls = []
        def action(cua):
            cua._run(["xdotool", "getactivewindow"])
            cua._run(["xdotool", "click", "1"])
        cua = self.fake(calls, action)
        state = nh.install_cua_guard(cua, env={}, checker=self.checker(3))
        with self.assertRaises(CuaError):
            cua.handle_cua_message({})
        self.assertEqual(calls, [["xdotool", "getactivewindow"]])
        self.assertFalse(state["input_started"])

    def test_postflight_failure_after_input_retains_uncertain_outcome(self):
        calls = []
        cua = self.fake(calls, lambda engine: engine._run(["xdotool", "type", "x"]))
        state = nh.install_cua_guard(cua, env={}, checker=self.checker(3))
        with self.assertRaises(CuaError):
            cua.handle_cua_message({})
        result = nh.decorate_response({"executed": True, "performed": True}, state, {})
        self.assertFalse(result["executed"])
        self.assertTrue(result["input_may_have_been_sent"])
        self.assertTrue(result["do_not_replay"])
        self.assertNotEqual(result.get("performed"), False)
        self.assertTrue(json.loads(result["output"])["result_discarded"])

    def test_observe_late_failure_does_not_claim_input(self):
        calls = []
        cua = self.fake(calls, lambda engine: engine._run(["import", "-window", "root", "fixture.png"]))
        state = nh.install_cua_guard(cua, env={}, checker=self.checker(3))
        with self.assertRaises(CuaError):
            cua.handle_cua_message({})
        result = nh.decorate_response({"executed": False}, state, {})
        self.assertFalse(result["input_may_have_been_sent"])
        self.assertFalse(result["performed"])

    def test_successful_input_is_reported_performed(self):
        calls = []
        cua = self.fake(calls, lambda engine: engine._run(["/usr/bin/xdotool", "key", "a"]))
        state = nh.install_cua_guard(cua, env={}, checker=lambda env: {})
        cua.handle_cua_message({})
        result = nh.decorate_response({"executed": True}, state, {})
        self.assertTrue(result["performed"])
        self.assertTrue(result["input_may_have_been_sent"])
        self.assertFalse(result["do_not_replay"])

    def test_captured_pointer_helper_rechecks_after_its_internal_query(self):
        calls = []
        cua = self.fake(calls)
        cua._run_v3_base = cua._run
        def helper(argv, timeout=15):
            cua._run_v3_base(["xdotool", "getmouselocation", "--shell"])
            return cua._run_v3_base(argv, timeout=timeout)
        cua._run = helper
        cua.handle_cua_message = lambda msg: cua._run(["xdotool", "mousemove", "1", "1"])
        state = nh.install_cua_guard(cua, env={}, checker=self.checker(4))
        with self.assertRaises(CuaError):
            cua.handle_cua_message({})
        self.assertEqual(calls, [["xdotool", "getmouselocation", "--shell"]])
        self.assertFalse(state["input_started"])

    def test_metadata_does_not_leak_to_next_non_cua_response(self):
        state = {"active": True, "input_started": True, "failed_code": ""}
        first = nh.decorate_response({"executed": True}, state, {})
        self.assertTrue(first["input_may_have_been_sent"])
        second = {"executed": True, "channel": "CONTROL-DIRECT"}
        self.assertEqual(nh.decorate_response(second, state, {}), second)
        self.assertNotIn("input_may_have_been_sent", second)

class NativeFramingTests(unittest.TestCase):
    def test_captured_host_protocol_rejects_unbound_display_without_input(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="fb-native-guard-") as home:
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8",
                   "FRAMEBRIDGE_EXPANDED_PREFIX": str(root), dc.BINDING: "{}"}
            message = json.dumps({"op": "cua", "action": "OBSERVE", "authorized": "YES",
                                  "target": "ASUS", "id": "r2-native-test"}).encode()
            proc = subprocess.run([sys.executable, str(root / "packaging/framebridge-native-expanded")],
                                  input=struct.pack("<I", len(message)) + message,
                                  env=env, capture_output=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace")[-500:])
            self.assertGreaterEqual(len(proc.stdout), 4)
            length = struct.unpack("<I", proc.stdout[:4])[0]
            self.assertEqual(len(proc.stdout), length + 4)
            result = json.loads(proc.stdout[4:])
            self.assertFalse(result["executed"])
            self.assertFalse(result["performed"])
            self.assertFalse(result["input_may_have_been_sent"])
            self.assertFalse(result["do_not_replay"])
            self.assertEqual(json.loads(result["output"])["error_code"], "CUA_DISPLAY_UNAVAILABLE")

class AdapterDispatchTests(unittest.TestCase):
    def adapter(self, response=None, error=None):
        adapter = SimpleNamespace()
        def roundtrip(payload, timeout_s=60):
            if error:
                raise error
            return dict(response or {"executed": True})
        def handle(request, job_id):
            native = adapter.native_cua_roundtrip(request["cua"], timeout_s=60)
            return {"ok": bool(native.get("executed")), "mode": "CUA"}
        adapter.native_cua_roundtrip = roundtrip
        adapter.handle_cua_request = handle
        return adapter

    def test_native_metadata_survives_original_adapter_mapping(self):
        adapter = self.adapter({"executed": False, "input_may_have_been_sent": True,
                                "do_not_replay": True})
        ad.install_adapter_guard(adapter, env={}, checker=lambda env: {})
        result = adapter.handle_cua_request({"cua": {"action": "KEY"}}, "fixture")
        self.assertTrue(result["input_may_have_been_sent"])
        self.assertTrue(result["do_not_replay"])

    def test_before_dispatch_failure_claims_no_input(self):
        adapter = self.adapter()
        def check(env):
            raise dc.DisplayContextError("CUA_FOREGROUND_SESSION_CHANGED")
        ad.install_adapter_guard(adapter, env={}, checker=check)
        result = adapter.handle_cua_request({"cua": {"action": "KEY"}}, "fixture")
        self.assertFalse(result["performed"])
        self.assertFalse(result["do_not_replay"])

    def test_timeout_after_dispatch_never_invites_input_replay(self):
        adapter = self.adapter(error=TimeoutError("native transport timed out"))
        ad.install_adapter_guard(adapter, env={}, checker=lambda env: {})
        result = adapter.handle_cua_request({"cua": {"action": "KEY"}}, "fixture")
        self.assertFalse(result["ok"])
        self.assertTrue(result["input_may_have_been_sent"])
        self.assertTrue(result["do_not_replay"])
        self.assertNotIn("performed", result)

    def test_observe_timeout_has_no_input_outcome(self):
        adapter = self.adapter(error=TimeoutError("native transport timed out"))
        ad.install_adapter_guard(adapter, env={}, checker=lambda env: {})
        result = adapter.handle_cua_request({"cua": {"action": "OBSERVE"}}, "fixture")
        self.assertFalse(result["input_may_have_been_sent"])
        self.assertFalse(result["performed"])

    def test_adapter_postflight_preserves_explicit_no_input(self):
        adapter = self.adapter({"executed": False, "input_may_have_been_sent": False})
        count = [0]
        def check(env):
            count[0] += 1
            if count[0] == 2:
                raise dc.DisplayContextError("CUA_FOREGROUND_SESSION_CHANGED")
        ad.install_adapter_guard(adapter, env={}, checker=check)
        result = adapter.handle_cua_request({"cua": {"action": "KEY"}}, "fixture")
        self.assertFalse(result["input_may_have_been_sent"])
        self.assertFalse(result["performed"])

if __name__ == "__main__":
    unittest.main()
