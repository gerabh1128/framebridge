"""READ keeps the original selector and dispatches to the confirmed UUID."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "expanded"))

from framebridge_expanded import broker, fleet
from framebridge_expanded.fleet_read_mcp import register_fleet_read_tools


NODE_ID = "731320ca-b3a5-4ae7-9598-2746adbc2162"
ALIAS = "gerameg-lab"
HOST = "GERAMEG"
REMOTE = {"node_id": NODE_ID, "alias": ALIAS, "registered_label": ALIAS,
          "hostname": None, "transport": "broker_outbound",
          "capability_profile": "original-readonly", "operational_transport_ready": True}


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self, *, annotations):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class FleetReadMcpReceiptTests(unittest.TestCase):
    def setUp(self):
        fleet.bootstrap_original()
        mcp = Registry()
        register_fleet_read_tools(mcp, lambda: {"ok": True, "nodes": [REMOTE]}, fleet.node_read)
        self.tools = mcp.tools

    def test_alias_and_uuid_both_dispatch_by_uuid_and_keep_requested_selector(self):
        for target in (ALIAS, NODE_ID):
            with self.subTest(target=target), \
                    mock.patch.object(fleet, "resolve_node", return_value=REMOTE) as resolve, \
                    mock.patch.object(broker, "request", return_value={
                        "ok": True, "executed_hostname": HOST,
                        "result": {"status": "ok", "context_text": "never copied"},
                    }) as request:
                out = self.tools["framebridge_fleet_node_system_status"](target)
            resolve.assert_called_once_with(target)
            request.assert_called_once_with(NODE_ID, "system.status", {}, timeout_s=30)
            self.assertTrue(out["ok"])
            self.assertEqual(out["requested_target"], target)
            self.assertEqual(out["resolved_target"], ALIAS)
            self.assertEqual(out["node_id"], NODE_ID)
            self.assertEqual(out["executed_hostname"], HOST)
            self.assertEqual(out["data"]["result"], {"status": "ok"})

    def test_inventory_retains_registered_node_with_unknown_hostname(self):
        out = self.tools["framebridge_fleet_nodes"]()
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["nodes"]), 1)
        node = out["nodes"][0]
        self.assertEqual(node["registered_label"], ALIAS)
        self.assertEqual(node["node_id"], NODE_ID)
        self.assertIsNone(node["hostname"])
        self.assertIs(node["hostname_verified"], False)
        self.assertTrue(node["transport_ready"])

    def test_unresolved_hostname_does_not_become_a_local_or_remote_route(self):
        with mock.patch.object(fleet, "descriptors", return_value=[REMOTE]), \
                mock.patch.object(fleet, "read_local") as local, \
                mock.patch.object(broker, "request") as remote:
            out = self.tools["framebridge_fleet_node_system_status"](HOST)
        self.assertFalse(out["ok"])
        self.assertEqual(out["requested_target"], HOST)
        self.assertEqual(out["routing_error"], "NODE_NOT_FOUND")
        self.assertNotIn("resolved_target", out)
        local.assert_not_called()
        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
