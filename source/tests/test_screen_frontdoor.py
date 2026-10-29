import base64
import hashlib
import importlib.util
import json
import math
import random
import shlex
import struct
import unittest
import zlib
from pathlib import Path

from framebridge_expanded.real_screen.screen_frontdoor import CHUNK_BYTES, ScreenClient, ScreenError, register_framebridge_screen_tools


CAPTURE_ID = "1" * 32
SESSION_ID = "screen_" + "2" * 32
NODE_ID = "12345678-1234-1234-1234-123456789abc"
OTHER_NODE_ID = "87654321-4321-4321-4321-cba987654321"


def png(width=100, height=40):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    source = random.Random(20261022)
    scanlines = b"".join(b"\0" + source.randbytes(width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


class Image:
    def __init__(self, data, format):
        self.data = data
        self.format = format


class MCP:
    def __init__(self):
        self.tools = {}
        self.annotations = {}

    def tool(self, annotations):
        def register(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = annotations
            return fn
        return register


class Executor:
    def __init__(self, variant=None, hostname="fb-central-lab"):
        self.data = png()
        self.variant = variant
        self.hostname = hostname
        self.requests = []

    def __call__(self, **kwargs):
        request = json.loads(base64.b64decode(shlex.split(kwargs["script"])[-1], validate=True))
        self.requests.append(request)
        digest = hashlib.sha256(self.data).hexdigest()
        manifest = {"capture_id": CAPTURE_ID, "node": self.hostname,
                    "bytes": len(self.data), "sha256": digest, "width": 100, "height": 40,
                    "source": "linux-framebuffer", "screen_type": "tty", "active_vt": "tty1",
                    "session_id": SESSION_ID, "full_screen": True, "authentic_pixels": True,
                    "path": "/private/internal.png"}
        if request.get("session_id"):
            manifest["requested_session_visible"] = request["session_id"] == SESSION_ID
        if self.variant == "node":
            manifest["node"] = "invalid hostname"
        elif self.variant == "source":
            manifest["source"] = "terminal-text-renderer"
        elif self.variant == "provenance":
            manifest["authentic_pixels"] = False
        elif self.variant == "dimensions":
            manifest["width"] = 99
        elif self.variant == "x11":
            manifest["source"] = "x11-root-XGetImage"
            manifest["screen_type"] = "x11"
        if request["op"] == "read_chunk":
            index = request["index"]
            data = self.data[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]
            result = {"capture_id": CAPTURE_ID, "index": index,
                      "total_chunks": math.ceil(len(self.data) / CHUNK_BYTES),
                      "bytes": len(self.data), "sha256": digest, "encoding": "base64",
                      "data": base64.b64encode(data).decode(),
                      "more": (index + 1) * CHUNK_BYTES < len(self.data)}
            if self.variant == "chunk_index":
                result["index"] = index + 1
            elif self.variant == "chunk_hash":
                result["sha256"] = "0" * 64
            elif self.variant == "chunk_more":
                result["more"] = not result["more"]
            elif self.variant == "chunk_encoding":
                result["data"] = "?invalid?"
            elif self.variant == "chunk_length":
                result["data"] = base64.b64encode(data[:-1]).decode()
            elif self.variant == "image_hash":
                changed = bytes([data[0] ^ 1]) + data[1:]
                result["data"] = base64.b64encode(changed).decode()
        else:
            result = manifest
        error = (self.variant in ("error_exit1", "uncertain_input") or
                 request["op"] in ("key", "text") and
                 request.get("session_id", SESSION_ID) != SESSION_ID)
        envelope = {"ok": False, "error": {"code": "SESSION_NOT_VISIBLE"}} if error else {"ok": True, "result": result}
        if self.variant == "uncertain_input":
            envelope["error"] = {"code": "INPUT_SENT_OBSERVE_FAILED", "performed": True,
                                 "capture_consumed": True, "observe_again": True,
                                 "capture_id": CAPTURE_ID, "cause": "SCREEN_IO_FAILED",
                                 "text": "MUST NOT ESCAPE"}
        outer = {"ok": not error, "exit_code": "1" if error else "0",
                 "target": kwargs["target"], "node_id": NODE_ID, "mcp_job_id": kwargs["job_id"],
                 "mcp_script_sha256": kwargs["sha256"],
                 "stdout": json.dumps(envelope), "stdout_truncated": self.variant == "truncated"}
        if self.variant == "outer_job":
            outer["mcp_job_id"] = "different"
        elif self.variant == "outer_hash":
            outer["mcp_script_sha256"] = "0" * 64
        elif self.variant == "outer_target":
            outer["target"] = "OTHER"
        elif self.variant == "outer_target_chunk" and request["op"] == "read_chunk":
            outer["target"] = "OTHER"
        elif self.variant == "outer_node_id_chunk" and request["op"] == "read_chunk":
            outer["node_id"] = OTHER_NODE_ID
        elif self.variant == "outer_node_id_missing":
            outer.pop("node_id")
        elif self.variant == "outer_node_id_invalid":
            outer["node_id"] = "invalid"
        return outer


class ScreenTests(unittest.TestCase):
    def client(self, variant=None):
        executor = Executor(variant)
        return ScreenClient(executor, Image), executor

    def test_native_image_return_preserves_bytes_and_omits_internal_path(self):
        client, executor = self.client()
        result = client.call("observe", "gerameg", True)
        self.assertEqual(result[0]["node"], "GERAMEG")
        self.assertEqual(result[1].data, executor.data)
        self.assertEqual(result[1].format, "png")
        self.assertNotIn("path", result[0]["result"])
        self.assertNotIn("image_base64", json.dumps(result[0]))
        self.assertGreater(len(executor.requests), 2)

    def test_authorization_required_before_any_exec(self):
        client, executor = self.client()
        self.assertEqual(client.call("observe", "GERAMEG", False)["error"], "AUTHORIZED_TRUE_REQUIRED")
        self.assertEqual(executor.requests, [])

    def test_invalid_node_cannot_reach_shell(self):
        client, executor = self.client()
        self.assertEqual(client.call("observe", "GERAMEG;id", True)["error"], "SCREEN_NODE_INVALID")
        self.assertEqual(executor.requests, [])

    def test_visible_action_requires_capture_id(self):
        client, executor = self.client()
        self.assertEqual(client.call("key", "GERAMEG", True, key="ENTER")["error"], "SCREEN_CAPTURE_ID_REQUIRED")
        self.assertEqual(executor.requests, [])

    def test_hidden_reference_observe_shows_full_screen_and_visibility_false(self):
        client, executor = self.client()
        result = client.call("observe", "GERAMEG", True, session_id="hidden_virtual_session")
        self.assertFalse(result[0]["result"]["requested_session_visible"])
        self.assertIsInstance(result[1], Image)

    def test_hidden_action_error_survives_exit1_without_image(self):
        client, executor = self.client()
        result = client.call("key", "GERAMEG", True, capture_id=CAPTURE_ID,
                             session_id="hidden_virtual_session", key="ENTER")
        self.assertEqual(result["error"], "SESSION_NOT_VISIBLE")
        self.assertEqual(len(executor.requests), 1)

    def test_backend_structured_error_survives_failure_flag_and_exit1(self):
        client, executor = self.client("error_exit1")
        self.assertEqual(client.call("observe", "GERAMEG", True)["error"], "SESSION_NOT_VISIBLE")

    def test_key_action_returns_new_real_image(self):
        client, executor = self.client()
        result = client.call("key", "GERAMEG", True, capture_id=CAPTURE_ID, key="CTRL_L")
        self.assertEqual(executor.requests[0]["op"], "key")
        self.assertEqual(result[1].data, executor.data)

    def test_uncertain_input_details_preserved_without_payload(self):
        client, executor = self.client("uncertain_input")
        result = client.call("key", "GERAMEG", True, capture_id=CAPTURE_ID, key="ENTER")
        self.assertEqual(result["error"], "INPUT_SENT_OBSERVE_FAILED")
        self.assertIs(result["performed"], True)
        self.assertIs(result["capture_consumed"], True)
        self.assertIs(result["observe_again"], True)
        self.assertEqual(result["capture_id"], CAPTURE_ID)
        self.assertNotIn("text", result)
        self.assertEqual(len(executor.requests), 1)

    def test_utf8_text_limit_matches_backend(self):
        client, executor = self.client()
        result = client.call("text", "GERAMEG", True, capture_id=CAPTURE_ID, text="界" * 1400)
        self.assertEqual(result["error"], "SCREEN_TEXT_INVALID")
        self.assertEqual(executor.requests, [])

    def test_text_is_base64_transport_and_not_shell_interpolation(self):
        client, executor = self.client()
        text = "literal $(touch /tmp/no) `id` ' quote"
        result = client.call("text", "GERAMEG", True, capture_id=CAPTURE_ID, text=text)
        self.assertEqual(executor.requests[0]["text"], text)
        self.assertIsInstance(result[1], Image)

    def test_integrity_rejections(self):
        expected = {"outer_job": "SCREEN_EXEC_BINDING_INVALID",
                    "outer_hash": "SCREEN_EXEC_BINDING_INVALID",
                    "outer_target": "SCREEN_EXEC_BINDING_INVALID",
                    "truncated": "SCREEN_STDOUT_INVALID", "node": "SCREEN_IMAGE_HOSTNAME_INVALID",
                    "outer_target_chunk": "SCREEN_EXEC_BINDING_INVALID",
                    "outer_node_id_chunk": "SCREEN_CHUNK_NODE_MISMATCH",
                    "outer_node_id_missing": "SCREEN_EXEC_NODE_ID_INVALID",
                    "outer_node_id_invalid": "SCREEN_EXEC_NODE_ID_INVALID",
                    "source": "SCREEN_PIXEL_SOURCE_INVALID", "provenance": "SCREEN_PIXEL_PROVENANCE_INVALID",
                    "dimensions": "SCREEN_PNG_INVALID", "chunk_index": "SCREEN_CHUNK_BINDING_INVALID",
                    "chunk_hash": "SCREEN_CHUNK_MANIFEST_INVALID", "chunk_more": "SCREEN_CHUNK_MANIFEST_INVALID",
                    "chunk_encoding": "SCREEN_CHUNK_INVALID", "chunk_length": "SCREEN_CHUNK_LENGTH_INVALID",
                    "image_hash": "SCREEN_IMAGE_HASH_MISMATCH"}
        for variant, code in expected.items():
            with self.subTest(variant=variant):
                client, _ = self.client(variant)
                self.assertEqual(client.call("observe", "GERAMEG", True)["error"], code)

    def test_generic_node_is_passed_to_existing_executor(self):
        client, executor = self.client()
        self.assertEqual(client.call("observe", "ASUS", True)[0]["node"], "ASUS")

    def test_exact_x11_capture_source_is_accepted(self):
        client, executor = self.client("x11")
        result = client.call("observe", "ASUS", True)
        self.assertEqual(result[0]["result"]["source"], "x11-root-XGetImage")
        self.assertEqual(result[1].data, executor.data)

    def test_trusted_alias_or_uuid_resolver_is_injected(self):
        executor = Executor()
        client = ScreenClient(executor, Image, lambda node: "GERAMEG")
        self.assertEqual(client.call("observe", "node-alias", True)[0]["node"], "GERAMEG")

    def test_registration_annotations_and_callable_functions(self):
        mcp = MCP()
        register_framebridge_screen_tools(mcp, Executor(), image_factory=Image)
        self.assertEqual(set(mcp.tools), {"framebridge_screen_observe", "framebridge_screen_key", "framebridge_screen_text"})
        self.assertTrue(mcp.annotations["framebridge_screen_observe"]["readOnlyHint"])
        self.assertFalse(mcp.annotations["framebridge_screen_key"]["readOnlyHint"])
        self.assertIsInstance(mcp.tools["framebridge_screen_observe"]("GERAMEG", True)[1], Image)

    def test_local_route_preserves_distinct_real_hostname_and_uuid(self):
        executor = Executor(hostname="fb-central-lab")
        client = ScreenClient(executor, Image, lambda alias: "LOCAL")
        result = client.call("observe", "LOCAL", True)
        self.assertEqual(result[0]["node"], "LOCAL")
        self.assertEqual(result[0]["hostname"], "fb-central-lab")
        self.assertEqual(result[0]["node_id"], NODE_ID)
        self.assertEqual(result[0]["result"]["node"], "fb-central-lab")
        self.assertEqual(result[0]["result"]["transport_node"], "LOCAL")
        self.assertEqual(result[1].data, executor.data)

    def test_remote_uuid_alias_and_hostname_remain_distinct(self):
        executor = Executor(hostname="fb-node-lab")
        client = ScreenClient(executor, Image, lambda alias: "fb-node")
        result = client.call("observe", NODE_ID, True)
        self.assertEqual(result[0]["requested_node"], NODE_ID)
        self.assertEqual(result[0]["node"], "fb-node")
        self.assertEqual(result[0]["hostname"], "fb-node-lab")
        self.assertEqual(result[0]["result"]["node"], "fb-node-lab")
        self.assertEqual(result[0]["result"]["transport_node"], "fb-node")
        self.assertEqual(result[1].data, executor.data)

    def test_screen_action_keeps_capture_and_session_with_distinct_hostname(self):
        executor = Executor(hostname="fb-node-lab")
        client = ScreenClient(executor, Image, lambda alias: "fb-node")
        result = client.call("key", "fb-node", True, capture_id=CAPTURE_ID,
                             session_id=SESSION_ID, key="ENTER")
        self.assertEqual(executor.requests[0]["capture_id"], CAPTURE_ID)
        self.assertEqual(executor.requests[0]["session_id"], SESSION_ID)
        self.assertEqual(result[0]["hostname"], "fb-node-lab")
        self.assertEqual(result[1].data, executor.data)

    def test_arbitrary_dictionary_cannot_be_treated_as_authenticated_manifest(self):
        client, _ = self.client()
        verified = client.remote("LOCAL", {"op": "observe"})
        with self.assertRaisesRegex(ScreenError, "SCREEN_IMAGE_NODE_MISMATCH"):
            client.image("LOCAL", dict(verified))

    def test_another_client_cannot_supply_authenticated_manifest(self):
        first, _ = self.client()
        second, _ = self.client()
        verified = first.remote("LOCAL", {"op": "observe"})
        with self.assertRaisesRegex(ScreenError, "SCREEN_IMAGE_NODE_MISMATCH"):
            second.image("LOCAL", verified)

    def test_authenticated_hostname_cannot_be_changed_after_transport_validation(self):
        client, _ = self.client()
        verified = client.remote("LOCAL", {"op": "observe"})
        verified["node"] = "forged-hostname"
        with self.assertRaisesRegex(ScreenError, "SCREEN_IMAGE_NODE_MISMATCH"):
            client.image("LOCAL", verified)

    def test_hostname_must_be_a_bounded_nonempty_string(self):
        for hostname in ("", None, 123, "invalid hostname", "x" * 81):
            with self.subTest(hostname=hostname):
                executor = Executor(hostname=hostname)
                client = ScreenClient(executor, Image, lambda alias: "LOCAL")
                self.assertEqual(client.call("observe", "LOCAL", True)["error"],
                                 "SCREEN_IMAGE_HOSTNAME_INVALID")



if __name__ == "__main__":
    unittest.main()
