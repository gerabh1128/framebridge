"""Routing contract tests; original read provider is exercised locally."""
import hashlib
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "expanded"))
from framebridge_expanded import fleet

LOCAL_ID = "c6d827a4-219f-48fc-82d8-f410176313ca"
REMOTE_ID = "6d88c5ae-343e-4413-a202-ccae2ae7694a"


class FleetRoutingTests(unittest.TestCase):
    def setUp(self):
        self.config = {"schema_version": 1, "role": "central", "name": "test-central", "node_id": LOCAL_ID}
        self.local_config = mock.patch.object(fleet, "local_config", return_value=self.config)
        self.local_config.start()
        self.remote = {"alias": "test-node", "node_id": REMOTE_ID, "hostname": "test-node",
                       "transport": "broker_outbound", "target": "test-node",
                       "source": "confirmed_pairing", "capability_profile": "original-readonly",
                       "operational_transport_ready": True}
        self.remote_nodes = mock.patch.object(fleet, "_remote_descriptors", return_value=[self.remote])
        self.remote_nodes.start()

    def tearDown(self):
        self.remote_nodes.stop()
        self.local_config.stop()

    def test_original_local_read_provider(self):
        value = fleet.node_read("LOCAL", "system.status", {})
        self.assertTrue(value["ok"])
        self.assertEqual(value["result"]["hostname"], socket.gethostname())
        self.assertEqual(value["node"], "test-central")
        self.assertEqual(value["transport"], "local")

    def test_original_closed_read_schema(self):
        value = fleet.node_read("LOCAL", "system.status", {"command": "uname"})
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"], "FLEET_READ_ARGUMENTS_INVALID")
        self.assertFalse(fleet.node_read("LOCAL", "exec", {})["ok"])

    def test_discovery_identity_and_alias_routing(self):
        value = fleet.discover_nodes()
        self.assertTrue(value["ok"])
        self.assertEqual({n["node_id"] for n in value["nodes"]}, {LOCAL_ID, REMOTE_ID})
        self.assertEqual(fleet.resolve_node("test-node")["node_id"], REMOTE_ID)
        self.assertEqual(fleet.resolve_node(REMOTE_ID)["alias"], "test-node")
        self.assertEqual(fleet.resolve_node("LOCAL")["node_id"], LOCAL_ID)

    def test_alias_collision_fails_closed(self):
        self.remote["alias"] = "test-central"
        value = fleet.discover_nodes()
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"], "FLEET_IDENTITY_COLLISION")

    def test_exec_guards_run_before_node_lookup(self):
        with mock.patch.object(fleet, "resolve_node", side_effect=AssertionError("must not resolve")):
            cases = [
                ({"authorized": False}, "AUTHORIZED_TRUE_REQUIRED"),
                ({"sha256": "0" * 64}, "SCRIPT_SHA256_MISMATCH"),
                ({"job_id": "../escape"}, "INVALID_JOB_ID"),
                ({"timeout_s": 0}, "INVALID_TIMEOUT"),
                ({"timeout_s": 601}, "INVALID_TIMEOUT"),
            ]
            for extra, expected in cases:
                kwargs = {"alias": "test-node", "script": "printf ok", "authorized": True}
                kwargs.update(extra)
                self.assertEqual(fleet.execute_node(**kwargs)["error"], expected)

    def test_exec_remote_binding(self):
        fake = types.ModuleType("framebridge_expanded.broker")
        calls = []
        def request(node_id, capability, args, timeout_s):
            calls.append((node_id, capability, args, timeout_s))
            return {"ok": True, "exit_code": "0", "job_id": args["job_id"],
                    "command_sha256": args["sha256"], "stdout": "safe"}
        fake.request = request
        with mock.patch("framebridge_expanded.broker", fake, create=True), mock.patch.dict(sys.modules, {"framebridge_expanded.broker": fake}):
            from framebridge_expanded import broker
            with mock.patch.object(broker, "request", side_effect=request):
                value = fleet.execute_node("test-node", "printf ok", True, job_id="test-job", include_stdout=True)
        self.assertTrue(value["ok"])
        self.assertEqual(value["target"], "test-node")
        self.assertEqual(value["mcp_script_sha256"], hashlib.sha256(b"printf ok").hexdigest())
        self.assertEqual(calls[0][0:2], (REMOTE_ID, "admin.exec"))

    def test_exec_rejects_mismatched_result(self):
        fake = types.ModuleType("framebridge_expanded.broker")
        fake.request = lambda *a, **k: {"ok": True, "job_id": "wrong", "command_sha256": "0"*64}
        with mock.patch("framebridge_expanded.broker", fake, create=True), mock.patch.dict(sys.modules, {"framebridge_expanded.broker": fake}):
            value = fleet.execute_node("test-node", "printf ok", True, job_id="bound-job")
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"], "NODE_EXEC_JOB_BINDING_INVALID")

    def test_cua_remote_original_wire_contract(self):
        fake = types.ModuleType("framebridge_expanded.broker")
        calls = []
        def request(node_id, capability, args, timeout_s):
            calls.append((node_id, capability, args, timeout_s))
            return {"ok": True, "job_id": args["job_id"], "frame_output": ""}
        fake.request = request
        with mock.patch("framebridge_expanded.broker", fake, create=True), mock.patch.dict(sys.modules, {"framebridge_expanded.broker": fake}):
            value = fleet.cua_node("test-node", {"action": "OBSERVE"}, True, job_id="cua-bound")
        self.assertTrue(value["ok"])
        self.assertEqual(calls[0][1], "admin.cua")
        self.assertEqual(calls[0][2]["mode"], "EXEC")
        self.assertEqual(calls[0][2]["request_type"], "cua")
        self.assertEqual(calls[0][2]["job_id"], "cua-bound")

    def test_local_alias_uses_original_adapter_without_network_envelope(self):
        def adapter(payload):
            return {"ok": True, "job_id": payload["job_id"],
                    "command_sha256": payload.get("sha256"), "stdout": "local"}
        with mock.patch("framebridge_expanded.runtime.adapter_request", side_effect=adapter) as call:
            result = fleet.execute_node("test-central", "printf ok", True, job_id="local-alias")
        self.assertTrue(result["ok"])
        self.assertEqual(result["target"], "LOCAL")
        self.assertEqual(call.call_args.args[0]["mode"], "EXEC")
        self.assertNotIn("request_id", call.call_args.args[0])

    def test_cua_limits_preserved(self):
        self.assertEqual(fleet.cua_node("test-node", {"action": "OBSERVE"}, True, 181)["error"],
                         "INVALID_CUA_TIMEOUT")
        self.assertEqual(fleet.cua_node("test-node", {"action": "OBSERVE"}, False)["error"],
                         "AUTHORIZED_TRUE_REQUIRED")

    def test_offline_node_never_queues_work(self):
        self.remote["operational_transport_ready"] = False
        self.assertEqual(fleet.execute_node("test-node", "printf ok", True)["error"], "NODE_OFFLINE")
        self.assertEqual(fleet.node_read("test-node", "system.status", {})["error"], "NODE_OFFLINE")


if __name__ == "__main__":
    unittest.main()
