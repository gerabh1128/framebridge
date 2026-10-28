"""Portable registration integration through the bundled FastMCP dispatcher.

Executors are bounded fixtures. No installed adapter, desktop or Fleet node is
contacted; these checks establish dispatch and identity wiring, not acceptance.
"""
import ast
import asyncio
import base64
from functools import partial, wraps
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import shlex
import socket
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

WORK = Path(__file__).resolve().parents[1]
VENDOR = WORK / "sources/original-geranas/FrameBridge-Fleet-Hub/vendor"
sys.path.insert(0, str(WORK / "expanded"))
sys.path.insert(0, str(VENDOR))
import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from framebridge_expanded import tty_mcp
from framebridge_expanded.real_screen.screen_frontdoor import register_framebridge_screen_tools
from framebridge_expanded.real_screen.screen_presentation import register_screen_presentation
from test_screen_frontdoor import Executor, NODE_ID, SESSION_ID

PORTABLE = WORK / "portable/front_door_mcp.py"


class ChunkExecutor(Executor):
    """Honor the typed route's optional larger chunk while retaining bindings."""
    def __call__(self, **kwargs):
        outer = super().__call__(**kwargs)
        request = json.loads(base64.b64decode(shlex.split(kwargs["script"])[-1]))
        if request["op"] == "read_chunk" and request.get("chunk_bytes"):
            size, index = request["chunk_bytes"], request["index"]
            result = json.loads(outer["stdout"])
            result["result"].update(
                total_chunks=math.ceil(len(self.data) / size),
                data=base64.b64encode(self.data[index * size:(index + 1) * size]).decode(),
                more=(index + 1) * size < len(self.data),
            )
            outer["stdout"] = json.dumps(result)
        return outer


def portable(*, execute=None, screen_execute=None, threaded=True):
    """Load the actual registration statements without bootstrap/profile effects."""
    tree = ast.parse(PORTABLE.read_text())
    names = {"_BlockingToolRegistrar", "_registered_operations", "framebridge_status",
             "framebridge_exec", "_screen_exec_request"}
    assignments = {"_blocking_tools", "_real_screen_client"}
    registrations = {"register_screen_presentation", "register_tty_tools"}
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in assignments for target in node.targets):
            if not threaded and node.targets[0].id == "_blocking_tools":
                node.value = ast.Name(id="mcp", ctx=ast.Load())
            selected.append(node)
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Name) and node.value.func.id in registrations):
            selected.append(node)
    mcp = FastMCP("dispatch fixture")
    execute = execute or (lambda *args: {"ok": True})
    screen_execute = screen_execute or (lambda *args: {"ok": False})
    namespace = dict(mcp=mcp, anyio=anyio, partial=partial, wraps=wraps, inspect=inspect,
                     ToolAnnotations=ToolAnnotations, Image=Image, socket=socket,
                     ADAPTER_SOCKET=Path("/nonexistent/framebridge-test.sock"),
                     PRODUCT_VERSION="test", GUIDE_SCHEMA=1,
                     _framebridge_exec_geranas_impl=execute, expanded_execute_node=execute,
                     expanded_execute_screen_node=screen_execute,
                     register_framebridge_screen_tools=register_framebridge_screen_tools,
                     resolve_profile_node=lambda target: target.upper(),
                     register_screen_presentation=register_screen_presentation,
                     register_tty_tools=tty_mcp.register_tools)
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                 str(PORTABLE), "exec"), namespace)
    return mcp, namespace


def structured(reply):
    if isinstance(reply, tuple):
        content, value = reply
        if value is not None:
            return value
        reply = content
    if isinstance(reply, dict):
        return reply
    return json.loads(next(item.text for item in reply if item.type == "text"))


async def until_set(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(.001)
    await asyncio.wait_for(poll(), 1)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_returns_status_while_exec_and_tty_capture_are_running(self):
        exec_started, capture_started, release = (threading.Event() for _ in range(3))
        fixture = ChunkExecutor()
        calls = []

        def execute(*args):
            calls.append(args)
            exec_started.set()
            release.wait(2)
            return {"ok": True, "marker": args[0]}

        def screen(alias, script, authorized, timeout_s, job_id, sha256, include_stdout):
            capture_started.set()
            release.wait(2)
            return fixture(target=alias, script=script, authorized=authorized,
                           timeout_s=timeout_s, job_id=job_id, sha256=sha256,
                           include_stdout=include_stdout)

        mcp, _ = portable(execute=execute, screen_execute=screen)
        slow = asyncio.create_task(mcp.call_tool("framebridge_exec", {
            "script": "bounded-marker", "authorized": True}))
        capture = asyncio.create_task(mcp.call_tool("cua_tty_observe", {
            "session_id": SESSION_ID, "authorized": True}))
        try:
            await until_set(exec_started)
            await until_set(capture_started)
            status = structured(await asyncio.wait_for(mcp.call_tool("framebridge_status", {}), .3))
            self.assertTrue(status["ok"])
            self.assertFalse(slow.done())
            self.assertFalse(capture.done())
        finally:
            release.set()
            exec_result, capture_result = await asyncio.gather(slow, capture)
        self.assertEqual(structured(exec_result)["marker"], "bounded-marker")
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(item.type == "image" for item in capture_result))

    async def test_fast_exec_response_is_delivered_before_slow_exec_completes(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def execute(*args):
            marker = args[0]
            calls.append(marker)
            if marker == "slow":
                started.set()
                release.wait(2)
            return {"ok": True, "marker": marker}

        mcp, _ = portable(execute=execute)
        slow = asyncio.create_task(mcp.call_tool("framebridge_exec", {
            "script": "slow", "authorized": True}))
        try:
            await until_set(started)
            fast = structured(await asyncio.wait_for(mcp.call_tool("framebridge_exec", {
                "script": "fast", "authorized": True}), .3))
            self.assertEqual(fast["marker"], "fast")
            self.assertFalse(slow.done())
        finally:
            release.set()
            slow_result = await slow
        self.assertEqual(structured(slow_result)["marker"], "slow")
        self.assertCountEqual(calls, ["slow", "fast"])

    async def test_full_portable_import_registers_tools_without_bootstrap_side_effects(self):
        import importlib.util
        from framebridge_expanded import fleet
        spec = importlib.util.spec_from_file_location("frontdoor_dispatch_test", PORTABLE)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(fleet, "bootstrap_original") as bootstrap:
            spec.loader.exec_module(module)
        bootstrap.assert_called_once_with()
        names = {tool.name for tool in await module.mcp.list_tools()}
        self.assertTrue(set(tty_mcp.TOOL_NAMES) <= names)
        self.assertTrue({"framebridge_exec", "framebridge_cua", "framebridge_screen_show"} <= names)
        self.assertFalse(inspect.iscoroutinefunction(module.framebridge_exec))

    async def test_schemas_metadata_and_internal_sync_callbacks_are_preserved(self):
        mcp, namespace = portable()
        baseline, _ = portable(threaded=False)
        actual = {tool.name: tool.model_dump() for tool in await mcp.list_tools()}
        expected = {tool.name: tool.model_dump() for tool in await baseline.list_tools()}
        self.assertEqual(actual, expected)
        self.assertFalse(inspect.iscoroutinefunction(namespace["framebridge_exec"]))
        self.assertFalse(inspect.iscoroutinefunction(namespace["_screen_exec_request"]))
        self.assertIsInstance(namespace["framebridge_exec"]("marker", True), dict)

    async def test_local_and_remote_tty_capture_use_identity_producing_transport(self):
        for target in ("LOCAL", "WORKER_1"):
            with self.subTest(target=target):
                fixture, calls = ChunkExecutor(), []

                def screen(alias, script, authorized, timeout_s, job_id, sha256, include_stdout):
                    calls.append(alias)
                    return fixture(target=alias, script=script, authorized=authorized,
                                   timeout_s=timeout_s, job_id=job_id, sha256=sha256,
                                   include_stdout=include_stdout)

                legacy = mock.Mock(side_effect=AssertionError("legacy EXEC must not capture"))
                mcp, _ = portable(execute=legacy, screen_execute=screen)
                result = await mcp.call_tool("cua_tty_observe", {
                    "session_id": SESSION_ID, "authorized": True, "target": target})
                self.assertTrue(any(item.type == "image" for item in result))
                self.assertEqual(set(calls), {target})
                self.assertGreaterEqual(len(calls), 2)
                legacy.assert_not_called()

    async def test_missing_uuid_and_wrong_reply_binding_still_fail_closed(self):
        for corruption, expected in (("uuid", "SCREEN_EXEC_NODE_ID_INVALID"),
                                     ("hash", "SCREEN_EXEC_BINDING_INVALID")):
            with self.subTest(corruption=corruption):
                fixture = ChunkExecutor()

                def screen(alias, script, authorized, timeout_s, job_id, sha256, include_stdout):
                    result = fixture(target=alias, script=script, authorized=authorized,
                                     timeout_s=timeout_s, job_id=job_id, sha256=sha256,
                                     include_stdout=include_stdout)
                    if corruption == "uuid":
                        result.pop("node_id")
                    else:
                        result["mcp_script_sha256"] = "0" * 64
                    return result

                mcp, _ = portable(screen_execute=screen)
                result = structured(await mcp.call_tool("cua_tty_observe", {
                    "session_id": SESSION_ID, "authorized": True}))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], expected)

    async def test_other_tty_rpcs_also_leave_status_responsive(self):
        started, release = threading.Event(), threading.Event()
        mcp, _ = portable()

        def request(*args):
            started.set()
            release.wait(2)
            return {"ok": True}

        with mock.patch.object(tty_mcp, "_remote_request", side_effect=request):
            slow = asyncio.create_task(mcp.call_tool("cua_tty_status", {
                "authorized": True, "target": "WORKER_1"}))
            try:
                await until_set(started)
                self.assertTrue(structured(await asyncio.wait_for(
                    mcp.call_tool("framebridge_status", {}), .3))["ok"])
                self.assertFalse(slow.done())
            finally:
                release.set()
                await slow

    async def test_anyio_cancellation_waits_for_single_worker_without_replay(self):
        started, release, finished = (threading.Event() for _ in range(3))
        mcp, namespace = portable()
        calls, scopes = [], []

        @namespace["_blocking_tools"].tool()
        def bounded_operation() -> dict:
            calls.append(1)
            started.set()
            release.wait(2)
            finished.set()
            return {"ok": True}

        async def dispatch():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await mcp.call_tool("bounded_operation", {})

        async with anyio.create_task_group() as group:
            group.start_soon(dispatch)
            await until_set(started)
            scopes[0].cancel()
            await anyio.sleep(.02)
            self.assertFalse(finished.is_set())
            release.set()
        self.assertTrue(finished.is_set())
        self.assertEqual(calls, [1])


class AutomaticJobTests(unittest.TestCase):
    def test_identical_concurrent_auto_jobs_are_distinct_and_explicit_ids_unchanged(self):
        from concurrent.futures import ThreadPoolExecutor
        tree = ast.parse(PORTABLE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_framebridge_exec_geranas_impl")
        namespace = dict(hashlib=hashlib, time=time, uuid=uuid,
                         JOB_RE=re.compile(r"^[A-Za-z0-9._-]{1,80}$"),
                         adapter_request=lambda payload: {"ok": True, "job_id": payload["job_id"]},
                         sanitize_text=lambda text: text)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(PORTABLE), "exec"), namespace)
        execute = namespace["_framebridge_exec_geranas_impl"]
        with mock.patch.object(time, "strftime", return_value="20261028-000000"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: execute("same-command", True), range(16)))
            self.assertEqual(len({result["job_id"] for result in results}), 16)
            self.assertTrue(all(namespace["JOB_RE"].fullmatch(result["job_id"]) for result in results))
            first = execute("same-command", True, job_id="owner-idempotent-id")
            second = execute("same-command", True, job_id="owner-idempotent-id")
        self.assertEqual(first["job_id"], "owner-idempotent-id")
        self.assertEqual(first["job_id"], second["job_id"])


if __name__ == "__main__":
    unittest.main()
