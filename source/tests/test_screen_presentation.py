"""Exercise original-artifact replay and the actual bundled MCP SDK contract."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import CallToolResult

from framebridge_expanded.real_screen import screen_rpc as rpc
from framebridge_expanded.real_screen.screen_capture import CaptureError
from framebridge_expanded.real_screen.screen_frontdoor import ScreenClient
from framebridge_expanded.real_screen.screen_presentation import (
    TEMPLATE_URI, VIEWER_HTML, register_screen_presentation,
)
from test_screen_frontdoor import Executor, CAPTURE_ID, NODE_ID, png


class SavedCaptureRpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.data = png()
        self.manifest = {
            "capture_id": CAPTURE_ID, "node": "fb-central-lab", "uid": os.getuid(),
            "source": "linux-framebuffer", "screen_type": "tty", "active_vt": "tty1",
            "bytes": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest(),
            "width": 100, "height": 40, "synthetic": False, "pixel_source_verified": True,
            "full_screen": True, "authentic_pixels": True, "control_available": True,
            "_action_consumed": True, "captured_at": "2026-10-24T12:00:00Z",
        }
        self.png = self.directory / (CAPTURE_ID + ".png")
        self.png.write_bytes(self.data)
        self.png.chmod(0o600)
        for suffix in (".json", ".bound.json"):
            rpc.write_record(self.directory / (CAPTURE_ID + suffix), self.manifest)
        self.initial = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in self.directory.iterdir()}

    def test_replay_survives_client_restart_and_consumed_input_without_observation(self):
        with patch.object(rpc, "observe", side_effect=AssertionError("new capture forbidden")), \
             patch.object(rpc, "act", side_effect=AssertionError("input forbidden")):
            result = rpc.dispatch({"op": "read_capture", "capture_id": CAPTURE_ID}, self.directory)
        self.assertTrue(result["ok"])
        result = result["result"]
        self.assertEqual(result["sha256"], self.manifest["sha256"])
        self.assertEqual(result["captured_at"], self.manifest["captured_at"])
        self.assertTrue(result["historical_capture"])
        self.assertFalse(result["control_available"])
        self.assertNotIn("_action_consumed", result)
        self.assertNotIn("path", result)
        self.assertEqual(self.initial, {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                      for p in self.directory.iterdir()})

    def test_replay_rejects_changed_png(self):
        self.png.write_bytes(self.data + b"corruption")
        with self.assertRaisesRegex(CaptureError, "CAPTURE_SHA256_MISMATCH"):
            rpc.dispatch({"op": "read_capture", "capture_id": CAPTURE_ID}, self.directory)

    def test_replay_rejects_symlink_record(self):
        record = self.directory / (CAPTURE_ID + ".bound.json")
        record.unlink()
        record.symlink_to(self.directory / (CAPTURE_ID + ".json"))
        with self.assertRaises((OSError, rpc.ScreenRpcError)):
            rpc.dispatch({"op": "read_capture", "capture_id": CAPTURE_ID}, self.directory)

    def test_replay_rejects_unrecognized_fields_and_unknown_reference(self):
        with self.assertRaisesRegex(rpc.ScreenRpcError, "UNKNOWN_REQUEST_FIELDS"):
            rpc.validate_request({"op": "read_capture", "capture_id": CAPTURE_ID, "text": "x"})
        with self.assertRaises((OSError, rpc.ScreenRpcError)):
            rpc.dispatch({"op": "read_capture", "capture_id": "f" * 32}, self.directory)


class ScreenPresentationSdkTests(unittest.TestCase):
    def setUp(self):
        self.executor = Executor()
        self.client = ScreenClient(self.executor, Image, lambda name: name)
        self.mcp = FastMCP("screen-presentation-test")
        self.show = register_screen_presentation(self.mcp, self.client)

    def test_actual_sdk_preserves_original_png_and_private_ui_metadata(self):
        response = asyncio.run(self.mcp.call_tool(
            "framebridge_screen_show",
            {"node": "LOCAL", "capture_id": CAPTURE_ID, "authorized": True}))
        self.assertIsInstance(response, CallToolResult)
        images = [c for c in response.content if c.type == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mimeType, "image/png")
        self.assertEqual(base64.b64decode(images[0].data), self.executor.data)
        self.assertEqual(response.meta["framebridgeImage"]["data"], images[0].data)
        self.assertEqual(response.structuredContent["node_id"], NODE_ID)
        self.assertEqual(response.structuredContent["result"]["capture_id"], CAPTURE_ID)
        self.assertEqual(response.structuredContent["result"]["sha256"],
                         hashlib.sha256(self.executor.data).hexdigest())
        self.assertEqual(self.executor.requests[0],
                         {"op": "read_capture", "capture_id": CAPTURE_ID})
        self.assertTrue(all(r["op"] in {"read_capture", "read_chunk"} for r in self.executor.requests))

    def test_actual_descriptor_declares_ui_readonly_and_structured_schema(self):
        tools = asyncio.run(self.mcp.list_tools())
        self.assertEqual(len(tools), 1)
        tool = tools[0]
        self.assertEqual(tool.meta["ui"]["resourceUri"], TEMPLATE_URI)
        self.assertEqual(tool.meta["openai/outputTemplate"], TEMPLATE_URI)
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertFalse(tool.annotations.destructiveHint)
        self.assertIn("ok", tool.outputSchema["properties"])
        self.assertIn("result", tool.outputSchema["properties"])

    def test_resource_contains_offline_viewer_and_integrity_validation(self):
        resources = asyncio.run(self.mcp.list_resources())
        self.assertEqual(len(resources), 1)
        self.assertEqual(str(resources[0].uri), TEMPLATE_URI)
        contents = list(asyncio.run(self.mcp.read_resource(TEMPLATE_URI)))
        self.assertEqual(contents[0].mime_type, "text/html;profile=mcp-app")
        self.assertEqual(contents[0].content, VIEWER_HTML)
        self.assertEqual(contents[0].meta["ui"]["csp"]["connectDomains"], [])
        self.assertNotIn("https://", VIEWER_HTML)
        self.assertNotIn("fetch(", VIEWER_HTML)
        self.assertIn('crypto.subtle.digest("SHA-256",bytes)', VIEWER_HTML)
        self.assertIn("ui/notifications/tool-result", VIEWER_HTML)
        self.assertIn("window.openai", VIEWER_HTML)
        self.assertIn("This saved capture does not show subsequent screen changes", VIEWER_HTML)

    def test_unauthorized_replay_never_calls_backend(self):
        response = self.show("LOCAL", CAPTURE_ID, False)
        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["error"], "AUTHORIZED_TRUE_REQUIRED")
        self.assertEqual(self.executor.requests, [])

    def test_replay_requires_exact_capture_reference(self):
        response = self.show("LOCAL", "f" * 32, True)
        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["error"], "SCREEN_CAPTURE_REFERENCE_MISMATCH")
        self.assertEqual(len(self.executor.requests), 1)

    def test_replay_rejects_invalid_capture_before_backend(self):
        response = self.show("LOCAL", "../invalid", True)
        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["error"], "SCREEN_CAPTURE_ID_REQUIRED")
        self.assertEqual(self.executor.requests, [])

    def test_replay_preserves_existing_uuid_binding_validation(self):
        def corrupted(**kwargs):
            result = self.executor(**kwargs)
            result.pop("node_id", None)
            return result
        response = register_screen_presentation(FastMCP("binding-test"), ScreenClient(corrupted, Image))(
            "LOCAL", CAPTURE_ID, True)
        self.assertTrue(response.isError)
        self.assertFalse(response.structuredContent["ok"])


if __name__ == "__main__":
    unittest.main()
