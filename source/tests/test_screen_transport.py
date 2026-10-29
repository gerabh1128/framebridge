"""Regression tests for the explicit typed screen RPC transport."""
import ast
from pathlib import Path
import unittest

PORTABLE = Path(__file__).resolve().parents[1] / "portable/front_door_mcp.py"

class PortableTypedRouteTests(unittest.TestCase):
    def test_screen_callback_uses_explicit_typed_route(self):
        calls = []
        def typed(*args):
            calls.append(("typed", args))
            return {"typed": True}
        def generic(*args):
            calls.append(("generic", args))
            return {"typed": False}
        source = ast.parse(PORTABLE.read_text())
        definitions = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "_screen_exec_request"]
        self.assertEqual(len(definitions), 1)
        scope = {"expanded_execute_node": generic, "expanded_execute_screen_node": typed}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(PORTABLE), "exec"), scope)
        result = scope["_screen_exec_request"](script="canonical-screen-fixture", authorized=True, timeout_s=30,
                                              job_id="screen-fixture", sha256="fixture-hash",
                                              include_stdout=True, target="worker-1")
        self.assertEqual([c[0] for c in calls], ["typed"])
        self.assertTrue(result["typed"])



import base64
import copy
import hashlib
import json
import os
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from framebridge_expanded import fleet, broker, node
from framebridge_expanded.real_screen import screen_transport as transport
from framebridge_expanded.real_screen.screen_frontdoor import ScreenClient, ScreenError, RPC_COMMAND
from framebridge_expanded.real_screen.screen_rpc import encode

NODE_ID = "12345678-1234-1234-1234-123456789abc"
CAPTURE_ID = "a" * 32


def make_payload(request=None):
    request = {"op": "observe"} if request is None else request
    argument = base64.b64encode(encode(request)).decode("ascii")
    script = RPC_COMMAND + argument + "\n"
    return {"version": 1, "mode": "EXEC", "authorized": True,
            "request_type": "screen_rpc", "script": script,
            "sha256": hashlib.sha256(script.encode()).hexdigest(),
            "job_id": "screen-fixture", "timeout_s": 1, "include_stdout": True}


def response(payload, envelope=None):
    envelope = {"ok": True, "result": {"fixture": True}} if envelope is None else envelope
    return {"ok": envelope["ok"], "job_id": payload["job_id"],
            "command_sha256": payload["sha256"], "exit_code": "0" if envelope["ok"] else "1",
            "stdout": encode(envelope).decode() + "\n", "stdout_truncated": False}


class TypedPayloadTests(unittest.TestCase):
    def test_canonical_operation_roundtrips_without_shell(self):
        payload = make_payload({"op": "status"})
        request, argument = transport.validate_payload(payload)
        self.assertEqual(request, {"op": "status"})
        self.assertEqual(base64.b64decode(argument), encode(request))

    def test_common_authorization_and_shape_rejections(self):
        valid = make_payload()
        mutations = [{"authorized": False}, {"version": True}, {"version": 2},
                     {"mode": "CUA"}, {"job_id": "invalid job"}, {"timeout_s": True},
                     {"timeout_s": 31}, {"timeout_s": 0}, {"include_stdout": False},
                     {"sha256": "0" * 64}, {"request_type": "SCREEN_RPC"},
                     {"unexpected": "not accepted"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = dict(valid, **mutation)
                with self.assertRaises(transport.ScreenTransportError):
                    transport.validate_payload(payload)
        payload = dict(valid)
        del payload["authorized"]
        with self.assertRaises(transport.ScreenTransportError):
            transport.validate_payload(payload)

    def test_no_noncanonical_script_or_unknown_operation(self):
        prefix = RPC_COMMAND
        bad = ["echo arbitrary\n", prefix + "not-base64\n",
               make_payload()["script"] + "echo extra\n",
               make_payload()["script"].rstrip("\n") + " extra\n",
               prefix + base64.b64encode(b'{ "op" : "status" }').decode() + "\n",
               prefix + base64.b64encode(encode({"op": "exec", "script": "echo bad"})).decode() + "\n",
               prefix + base64.b64encode(encode({"op": "status", "path": "/tmp/other"})).decode() + "\n",
               prefix + "'" + base64.b64encode(encode({"op": "status"})).decode() + "'\n",
               prefix + base64.b64encode(b"x" * 16385).decode() + "\n"]
        for script in bad:
            with self.subTest(script=script[:100]):
                payload = make_payload()
                payload.update(script=script, sha256=hashlib.sha256(script.encode()).hexdigest())
                with self.assertRaises(transport.ScreenTransportError):
                    transport.validate_payload(payload)

    def test_existing_input_schema_is_preserved(self):
        for request in [{"op": "key", "capture_id": CAPTURE_ID, "key": "ENTER"},
                        {"op": "text", "capture_id": CAPTURE_ID, "text": "printf"},
                        {"op": "read_chunk", "capture_id": CAPTURE_ID, "index": 0, "expected_sha256": "f" * 64}]:
            with self.subTest(op=request["op"]):
                parsed, _ = transport.validate_payload(make_payload(request))
                self.assertEqual(parsed, request)
        with self.assertRaises(transport.ScreenTransportError):
            transport.validate_payload(make_payload({"op": "text", "capture_id": CAPTURE_ID, "text": "\x00"}))

    def test_adapter_intercepts_only_explicit_type(self):
        original = Mock(return_value={"visible_engine": True})
        adapter = SimpleNamespace(handle=original)
        transport.install_adapter_route(adapter)
        generic = make_payload()
        del generic["request_type"]
        self.assertEqual(adapter.handle(generic), {"visible_engine": True})
        original.assert_called_once_with(generic)
        original.reset_mock()
        with self.assertRaises(transport.ScreenTransportError):
            adapter.handle(dict(make_payload(), authorized=False))
        original.assert_not_called()

    def test_malformed_typed_case_cannot_fall_back(self):
        original = Mock(side_effect=AssertionError("no visible FBTERM fallback"))
        adapter = SimpleNamespace(handle=original)
        transport.install_adapter_route(adapter)
        for mutation in [{"request_type": " SCREEN_RPC "}, {"script": "echo arbitrary\n"},
                         {"sha256": "0" * 64}, {"mode": "INVALID"}, {"version": False}]:
            with self.subTest(mutation=mutation):
                with self.assertRaises(transport.ScreenTransportError):
                    adapter.handle(dict(make_payload(), **mutation))
        original.assert_not_called()

    def test_success_and_child_error_envelopes_remain_exact(self):
        payload = make_payload()
        good = {"ok": True, "result": {"fixture": True}}
        error = {"ok": False, "error": {"code": "INPUT_SENT_OBSERVE_FAILED", "performed": True,
                                      "capture_consumed": True, "observe_again": True,
                                      "capture_id": CAPTURE_ID, "cause": "SCREEN_IO_FAILED"}}
        for envelope in [good, error]:
            with self.subTest(ok=envelope["ok"]):
                runner = Mock(return_value=(0 if envelope["ok"] else 1, encode(envelope) + b"\n", b""))
                result = transport.handle_request(payload, runner=runner)
                self.assertEqual(result["stdout"].encode(), encode(envelope) + b"\n")
                self.assertEqual(result["job_id"], payload["job_id"])
                self.assertEqual(result["command_sha256"], payload["sha256"])
                self.assertEqual(result["ok"], envelope["ok"])
                runner.assert_called_once_with(transport.validate_payload(payload)[1], 1)

    def test_input_timeout_is_unknown_and_never_replayed(self):
        payload = make_payload({"op": "key", "capture_id": CAPTURE_ID, "key": "ENTER"})
        runner = Mock(side_effect=transport.ScreenProcessError("SCREEN_RPC_TIMEOUT", started=True))
        result = transport.handle_request(payload, runner=runner)
        error = json.loads(result["stdout"])["error"]
        self.assertEqual(error["code"], "SCREEN_RPC_TIMEOUT")
        self.assertTrue(error["observe_again"])
        self.assertEqual(error["capture_id"], CAPTURE_ID)
        self.assertNotIn("performed", error)
        self.assertNotIn("capture_consumed", error)
        runner.assert_called_once()

    def test_prelaunch_or_readonly_timeout_does_not_invent_input(self):
        for request, started in [({"op": "observe"}, True),
                                 ({"op": "key", "capture_id": CAPTURE_ID, "key": "ENTER"}, False)]:
            with self.subTest(request=request, started=started):
                runner = Mock(side_effect=transport.ScreenProcessError("SCREEN_RPC_PROCESS_FAILED", started=started))
                result = transport.handle_request(make_payload(request), runner=runner)
                error = json.loads(result["stdout"])["error"]
                self.assertNotIn("performed", error)
                self.assertNotIn("capture_consumed", error)
                self.assertNotIn("observe_again", error)
                runner.assert_called_once()

    def test_invalid_or_oversized_input_reply_is_uncertain(self):
        payload = make_payload({"op": "text", "capture_id": CAPTURE_ID, "text": "printf"})
        for output in [b"not JSON\n", b"x" * 4097]:
            with self.subTest(size=len(output)):
                runner = Mock(return_value=(0, output, b""))
                result = transport.handle_request(payload, runner=runner)
                error = json.loads(result["stdout"])["error"]
                self.assertTrue(error["observe_again"])
                self.assertNotIn("performed", error)
                self.assertNotIn("capture_consumed", error)
                self.assertLess(len(result["stdout"].encode()), 4096)
                runner.assert_called_once()


class BoundedSubprocessTests(unittest.TestCase):
    def launch_fixture(self, text, argument, timeout=1):
        real_popen = subprocess.Popen
        children, calls = [], []
        def fixture(argv, **kwargs):
            calls.append((argv, kwargs))
            child = real_popen(["/usr/bin/python3", "-B", "-c", text], **kwargs)
            children.append(child)
            return child
        with patch.object(transport.subprocess, "Popen", side_effect=fixture):
            try:
                result = transport._run_rpc(argument, timeout)
                return result, children, calls
            finally:
                self.assertTrue(all(child.poll() is not None for child in children))

    def test_fixed_argv_inherits_normal_environment_without_shell(self):
        argument = transport.validate_payload(make_payload())[1]
        output = encode({"ok": True, "result": {"fixture": True}}) + b"\n"
        result, children, calls = self.launch_fixture("import sys;sys.stdout.buffer.write(" + repr(output) + ")", argument)
        self.assertEqual(result, (0, output, b""))
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["/usr/bin/python3", "-B", "-m", "framebridge_expanded.real_screen.screen_rpc", argument])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("env", kwargs)
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(len(children), 1)

    def test_output_limit_does_not_accumulate_or_return_large_output(self):
        for stream in ["stdout", "stderr"]:
            with self.subTest(stream=stream):
                with self.assertRaisesRegex(transport.ScreenProcessError, "SCREEN_RPC_OUTPUT_LIMIT"):
                    self.launch_fixture("import sys;sys." + stream + ".buffer.write(b'x'*100000);sys." + stream + ".flush()", "fixture")

    def test_wall_timeout_kills_and_reaps_single_child(self):
        started = time.monotonic()
        with self.assertRaisesRegex(transport.ScreenProcessError, "SCREEN_RPC_TIMEOUT"):
            self.launch_fixture("import time;time.sleep(5)", "fixture", timeout=1)
        self.assertLess(time.monotonic() - started, 3)

    def test_timeout_cleans_owned_group_after_leader_exits(self):
        import signal
        import tempfile
        real_popen = subprocess.Popen
        children = []
        with tempfile.TemporaryDirectory(prefix="framebridge-screen-test-") as folder:
            pidfile = Path(folder) / "descendant.pid"
            script = ("import os,time\npid=os.fork()\n"
                      "if pid==0:\n time.sleep(5)\n os._exit(0)\n"
                      "open(" + repr(str(pidfile)) + ",'w').write(str(pid))\nos._exit(0)\n")
            def launch(argv, **kwargs):
                child = real_popen(["/usr/bin/python3", "-B", "-c", script], **kwargs)
                children.append(child)
                return child
            try:
                with patch.object(transport.subprocess, "Popen", side_effect=launch):
                    with self.assertRaisesRegex(transport.ScreenProcessError, "SCREEN_RPC_TIMEOUT"):
                        transport._run_rpc("fixture", 1)
                descendant = int(pidfile.read_text())
                def state():
                    try:
                        return (Path("/proc") / str(descendant) / "stat").read_text().rsplit(")", 1)[1].split()[0]
                    except FileNotFoundError:
                        return None
                deadline = time.monotonic() + 0.5
                while state() not in (None, "Z") and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIn(state(), (None, "Z"), "owned descendant survived the RPC timeout")
            finally:
                # Keep the deliberately failing pre-fix regression free of live orphans.
                for child in children:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=1)



class TypedFleetRouteTests(unittest.TestCase):
    def local_node(self):
        return {"transport": "local", "alias": "central", "node_id": NODE_ID}

    def remote_node(self):
        return {"transport": "broker_outbound", "alias": "worker-1", "node_id": NODE_ID,
                "operational_transport_ready": True}

    def execute(self, payload):
        return fleet.execute_screen_node("worker-1", payload["script"], True, payload["timeout_s"],
                                         payload["job_id"], payload["sha256"], True)

    def test_local_uses_typed_adapter_and_keeps_job_hash_uuid(self):
        payload = make_payload({"op": "status"})
        def adapter(value):
            self.assertEqual(value, payload)
            return response(value)
        with patch.object(fleet, "resolve_node", return_value=self.local_node()), \
             patch("framebridge_expanded.runtime.adapter_request", side_effect=adapter) as call:
            result = self.execute(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(result["target"], "LOCAL")
        self.assertEqual(result["node_id"], NODE_ID)
        self.assertEqual(result["mcp_job_id"], payload["job_id"])
        self.assertEqual(result["mcp_script_sha256"], payload["sha256"])
        call.assert_called_once()

    def test_remote_admin_exec_preserves_type_through_broker_node(self):
        payload = make_payload({"op": "status"})
        calls = []
        def adapter(value):
            self.assertEqual(value, payload)
            return response(value)
        def request(node_id, capability, value, timeout_s):
            calls.append((node_id, capability, value, timeout_s))
            req = {"node_id": node_id, "expires_at": time.time() + 30,
                   "request_id": "request-fixture", "operation": capability, "payload": value}
            with patch.object(node, "_context", return_value=(None, None, None, {"node_id": NODE_ID})):
                return node.dispatch_request(req, adapter_request=adapter)
        with patch.object(fleet, "resolve_node", return_value=self.remote_node()), \
             patch.object(broker, "request", side_effect=request):
            result = self.execute(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(result["node_id"], NODE_ID)
        self.assertEqual(result["target"], "worker-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "admin.exec")
        self.assertEqual(calls[0][2]["request_type"], "screen_rpc")

    def test_old_adapter_is_reported_without_shell_retry(self):
        remote = self.remote_node()
        legacy = {"ok": False, "adapter_error": "ValueError:UNSUPPORTED_REQUEST_TYPE"}
        def execute(**kwargs):
            target = kwargs.pop("target")
            return fleet.execute_screen_node(target, **kwargs)
        client = ScreenClient(execute, image_factory=lambda **kwargs: None)
        with patch.object(fleet, "resolve_node", return_value=remote), \
             patch.object(broker, "request", return_value=legacy) as request, \
             patch.object(fleet, "execute_node", side_effect=AssertionError("no legacy fallback")) as generic:
            with self.assertRaisesRegex(ScreenError, "SCREEN_RPC_UNSUPPORTED_ON_NODE"):
                client.remote("worker-1", {"op": "status"})
        request.assert_called_once()
        generic.assert_not_called()

    def test_bad_job_or_hash_response_is_rejected(self):
        payload = make_payload()
        for field in ["job_id", "command_sha256"]:
            with self.subTest(field=field):
                result = response(payload)
                result[field] = "invalid"
                with patch.object(fleet, "resolve_node", return_value=self.remote_node()), \
                     patch.object(broker, "request", return_value=result):
                    final = self.execute(payload)
                self.assertFalse(final["ok"])
                self.assertIn("BINDING_INVALID", final["error"])

    def test_offline_or_malformed_typed_payload_never_routes(self):
        payload = make_payload()
        for malformed in [dict(payload, script="echo not a screen RPC\n"),
                          dict(payload, sha256="0" * 64)]:
            with self.subTest(malformed=malformed), patch.object(fleet, "resolve_node") as resolve:
                result = self.execute(malformed)
                self.assertFalse(result["ok"])
                resolve.assert_not_called()
        with patch.object(fleet, "resolve_node", return_value=dict(self.remote_node(), operational_transport_ready=False)), \
             patch.object(broker, "request") as request:
            result = self.execute(payload)
            self.assertEqual(result["error"], "NODE_OFFLINE")
            request.assert_not_called()

    def test_generic_execute_remains_without_typed_marker(self):
        payload = make_payload({"op": "status"})
        calls = []
        def adapter(value):
            calls.append(value)
            self.assertNotIn("request_type", value)
            return response(value)
        with patch.object(fleet, "resolve_node", return_value=self.local_node()), \
             patch("framebridge_expanded.runtime.adapter_request", side_effect=adapter):
            result = fleet.execute_node("LOCAL", payload["script"], True, 1, payload["job_id"], payload["sha256"], True)
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)

if __name__ == "__main__":
    unittest.main()
