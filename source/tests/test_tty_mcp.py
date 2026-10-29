"""Real MCP transport tests with real local Bash consoles and a bounded executor boundary.

The TEST_NODE executor runs on this QA host in another private profile; this
tests remote RPC framing, not a second machine. Actual multi-node VM coverage is separate.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest

WORK = Path(__file__).resolve().parents[1]
VENDOR = WORK / "sources/original-geranas/FrameBridge-Fleet-Hub/vendor"
sys.path.insert(0, str(WORK / "expanded"))
sys.path.insert(0, str(VENDOR))
os.environ["PYTHONPATH"] = str(WORK / "expanded") + os.pathsep + str(VENDOR)
os.environ["FRAMEBRIDGE_EXPANDED_PREFIX"] = str(WORK)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from framebridge_expanded import tty
from framebridge_expanded.tty_mcp import TOOL_NAMES, _remote_request, register_tools
from unittest import mock
from framebridge_expanded.real_screen.screen_frontdoor import ScreenClient


SERVER = r'''
from pathlib import Path
import json,os,subprocess
from mcp.server.fastmcp import FastMCP
from framebridge_expanded.tty_mcp import register_tools
port=int(os.environ["QA_TTY_MCP_PORT"])
mcp=FastMCP("Real CUA-TTY QA",host="127.0.0.1",port=port,streamable_http_path="/mcp")
def execute(script,authorized,timeout_s,job_id,sha256,include_stdout,target):
    if authorized is not True or target!="TEST_NODE":
        return {"ok":False,"error":"QA_TARGET_INVALID"}
    env=os.environ.copy()
    env["FRAMEBRIDGE_EXPANDED_STATE"]=env["QA_TTY_REMOTE_STATE"]
    p=subprocess.run(["/bin/bash","--noprofile","--norc","-c",script],env=env,
                     stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout_s)
    with open(os.environ["QA_TTY_CALL_LOG"],"a") as log:
        log.write(json.dumps({"read":" framebridge_expanded.tty read " in (" "+script+" "),
                              "stdout_bytes":len(p.stdout.encode())})+"\n")
    return {"ok":True,"stdout":p.stdout[:4096],"stdout_truncated":len(p.stdout)>4096,
            "exit_code":str(p.returncode)}
class ScreenFixture:
    def call(self, op, target, authorized, **arguments):
        return {"ok":True,"test_fixture":"screen-delegation-only",
                "op":op,"target":target,"arguments":arguments}
register_tools(mcp,execute,screen_client=ScreenFixture())
mcp.run(transport="streamable-http")
'''


class RealMcpTtyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="tty-mcp-", dir=WORK / "qa"))
        cls.local = cls.root / "local"
        cls.remote = cls.root / "remote"
        cls.local.mkdir(mode=0o700)
        cls.remote.mkdir(mode=0o700)
        assert tty.start(cls.local).get("ok")
        assert tty.start(cls.remote).get("ok")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        child = cls.root / "server.py"
        child.write_text(SERVER)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(WORK / "expanded") + os.pathsep + str(VENDOR)
        env.pop("DISPLAY", None)
        env.pop("XDG_SESSION_TYPE", None)
        env["FRAMEBRIDGE_EXPANDED_STATE"] = str(cls.local)
        env["QA_TTY_REMOTE_STATE"] = str(cls.remote)
        env["QA_TTY_MCP_PORT"] = str(cls.port)
        cls.call_log = cls.root / "executor-calls.jsonl"
        env["QA_TTY_CALL_LOG"] = str(cls.call_log)
        log = (cls.root / "mcp-server.log").open("ab")
        cls.server = subprocess.Popen([sys.executable, str(child)], env=env,
                                      stdin=subprocess.DEVNULL, stdout=log,
                                      stderr=subprocess.STDOUT)
        log.close()
        ready = False
        deadline = time.monotonic() + 8
        while cls.server.poll() is None and time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", cls.port)) == 0:
                    ready = True
                    break
            time.sleep(0.05)
        if not ready:
            tty.stop(cls.local)
            tty.stop(cls.remote)
            cls.server.wait(timeout=2)
            raise AssertionError("Private MCP server failed to start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=2)
        assert tty.stop(cls.local).get("ok")
        assert tty.stop(cls.remote).get("ok")

    async def call(self, session, name, args):
        result = await session.call_tool(name, args)
        self.assertFalse(result.isError)
        value = getattr(result, "structuredContent", None)
        if not isinstance(value, dict):
            value = next(json.loads(c.text) for c in result.content if c.type == "text")
        return value

    def remote_request(self, payload):
        def execute(script, authorized, timeout_s, job_id, sha256, include_stdout, target):
            if authorized is not True or target != "TEST_NODE":
                return {"ok": False, "error": "QA_TARGET_INVALID"}
            env = os.environ.copy()
            env["FRAMEBRIDGE_EXPANDED_STATE"] = str(self.remote)
            process = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script],
                env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout_s)
            with self.call_log.open("a") as log:
                log.write(json.dumps({"read": " framebridge_expanded.tty read " in (" " + script + " "),
                                      "stdout_bytes": len(process.stdout.encode())}) + "\n")
            return {"ok": True, "stdout": process.stdout[:4096],
                    "stdout_truncated": len(process.stdout) > 4096, "exit_code": str(process.returncode)}
        return _remote_request(execute, "TEST_NODE", payload)

    async def test_lifecycle_over_mcp_and_persistent_bash_over_raw_rpc(self):
        async with streamablehttp_client("http://127.0.0.1:" + str(self.port) + "/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listing = await session.list_tools()
                self.assertEqual(sorted(t.name for t in listing.tools), sorted(TOOL_NAMES))
                denied = await self.call(session, "cua_tty_open", {"authorized": False})
                self.assertEqual(denied["error"], "AUTHORIZED_TRUE_REQUIRED")
                status = await self.call(session, "cua_tty_status", {"authorized": True})
                self.assertTrue(status["ok"])
                opened = await self.call(session, "cua_tty_open", {"authorized": True})
                self.assertTrue(opened["ok"])
                sid = opened["result"]["session_id"]
                try:
                    marker = self.local / "mcp-local-marker.txt"
                    command = "printf local_mcp_real > " + shlex.quote(str(marker))
                    typed = tty.request(self.local, {"op": "text.type", "authorized": True,
                                                     "session_id": sid, "text": command})
                    self.assertTrue(typed["ok"])
                    keyed = tty.request(self.local, {"op": "key.send", "authorized": True,
                                                     "session_id": sid, "key": "ENTER"})
                    self.assertTrue(keyed["ok"])
                    deadline = time.monotonic() + 3
                    while not marker.exists() and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    self.assertEqual(marker.read_text(), "local_mcp_real")
                    resized = await self.call(session, "cua_tty_resize",
                                             {"authorized": True, "session_id": sid, "rows": 33, "cols": 120})
                    self.assertTrue(resized["ok"])
                    observed = await self.call(session, "cua_tty_observe",
                                              {"authorized": True, "session_id": sid})
                    self.assertEqual(observed["test_fixture"], "screen-delegation-only")
                    self.assertEqual(observed["op"], "observe")
                    self.assertEqual(observed["arguments"]["session_id"], sid)
                    transcript = tty.request(self.local, {"op": "observe", "authorized": True, "session_id": sid})
                    self.assertTrue(transcript["result"]["real_terminal"])
                    self.assertEqual(transcript["result"]["screen"]["rows"], 33)
                finally:
                    closed = await self.call(session, "cua_tty_close",
                                             {"authorized": True, "session_id": sid})
                    self.assertTrue(closed["result"]["cleanup_verified"])

    async def test_remote_executor_framing_preserves_screen_larger_than_stdout_limit(self):
        async with streamablehttp_client("http://127.0.0.1:" + str(self.port) + "/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                opened = await self.call(session, "cua_tty_open",
                                         {"authorized": True, "target": "TEST_NODE", "rows": 32, "cols": 160})
                self.assertTrue(opened["ok"], opened)
                sid = opened["result"]["session_id"]
                try:
                    marker = self.remote / "random-screen-done.txt"
                    code = ("import random,string; from pathlib import Path; r=random.Random(23); "
                            "[print(''.join(r.choice(string.ascii_letters+string.digits) for _ in range(150))) "
                            "for _ in range(28)]; Path(" + repr(str(marker)) + ").write_text('done')")
                    command = "python3 -c " + shlex.quote(code)
                    self.assertTrue(self.remote_request(
                        {"op": "text.type", "authorized": True, "session_id": sid, "text": command})["ok"])
                    self.assertTrue(self.remote_request(
                        {"op": "key.send", "authorized": True, "session_id": sid, "key": "ENTER"})["ok"])
                    deadline = time.monotonic() + 3
                    while not marker.exists() and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    self.assertEqual(marker.read_text(), "done")
                    # Raw observe is a terminal text transcript. Physical MCP observe
                    # is separately delegated to ScreenClient and never used here.
                    value = self.remote_request({"op": "observe", "authorized": True, "session_id": sid})
                    self.assertTrue(value["ok"], value)
                    self.assertTrue(value["result"]["real_terminal"])
                    self.assertGreater(len(json.dumps(value)), 4096)
                    records = [json.loads(line) for line in self.call_log.read_text().splitlines()]
                    self.assertTrue(any(r["read"] for r in records))
                    self.assertTrue(all(r["stdout_bytes"] < 4096 for r in records))
                finally:
                    closed = await self.call(session, "cua_tty_close",
                        {"authorized": True, "target": "TEST_NODE", "session_id": sid})
                    self.assertTrue(closed["result"]["cleanup_verified"])


class FixtureMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register

class McpScreenBindingTests(unittest.TestCase):
    SID = "a" * 32
    CAPTURE = "b" * 32

    def tools(self, error="SESSION_NOT_VISIBLE", *, wrong_target=False):
        calls = []
        def execute(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "exit_code": "0", "stdout_truncated": False,
                    "target": "different-node" if wrong_target else kwargs["target"],
                    "node_id": "12345678-1234-1234-1234-123456789abc",
                    "mcp_job_id": kwargs["job_id"], "mcp_script_sha256": kwargs["sha256"],
                    "stdout": json.dumps({"ok": False, "error": error})}
        client = ScreenClient(execute, image_factory=lambda **kwargs: kwargs, resolve_node=lambda node: node)
        mcp = FixtureMcp()
        register_tools(mcp, execute, screen_client=client)
        return mcp.tools, calls

    def test_seven_existing_names_remain_registered(self):
        tools, _calls = self.tools()
        self.assertEqual(set(tools), set(TOOL_NAMES))

    def test_missing_capture_rejected_before_any_backend_execution(self):
        tools, calls = self.tools()
        with mock.patch.object(tty, "request") as raw:
            value = tools["cua_tty_key_send"](self.SID, "ENTER", True, "NodeCase")
        self.assertEqual(value["error"], "SCREEN_CAPTURE_ID_REQUIRED")
        self.assertEqual(calls, [])
        raw.assert_not_called()

    def test_unapproved_screen_observation_does_not_reach_backend(self):
        tools, calls = self.tools()
        value = tools["cua_tty_observe"](self.SID, False, "NodeCase")
        self.assertEqual(value["error"], "AUTHORIZED_TRUE_REQUIRED")
        self.assertEqual(calls, [])

    def test_hidden_session_error_has_no_raw_pty_fallback(self):
        tools, calls = self.tools()
        with mock.patch.object(tty, "request") as raw:
            value = tools["cua_tty_text_type"](self.SID, "not executed", True, "NodeCase", self.CAPTURE)
        self.assertEqual(value["error"], "SESSION_NOT_VISIBLE")
        self.assertEqual(calls[0]["target"], "NodeCase")
        raw.assert_not_called()

    def test_mismatched_executor_target_cannot_satisfy_screen_request(self):
        tools, _calls = self.tools(wrong_target=True)
        value = tools["cua_tty_key_send"](self.SID, "ENTER", True, "NodeCase", self.CAPTURE)
        self.assertEqual(value["error"], "SCREEN_EXEC_BINDING_INVALID")

    def test_graphical_input_rejection_is_preserved_without_hidden_fallback(self):
        tools, _calls = self.tools(error="USE_GRAPHICAL_CUA")
        with mock.patch.object(tty, "request") as raw:
            value = tools["cua_tty_key_send"](self.SID, "ENTER", True, "NodeCase", self.CAPTURE)
        self.assertEqual(value["error"], "USE_GRAPHICAL_CUA")
        raw.assert_not_called()

    def test_observe_delegation_preserves_requested_session_and_node(self):
        record = []
        class ScreenFixture:
            def call(self, *args, **kwargs):
                record.append((args, kwargs))
                return {"test_fixture": "delegation-only"}
        mcp = FixtureMcp()
        register_tools(mcp, lambda **_kw: self.fail("raw execute is not the observe path"),
                       screen_client=ScreenFixture())
        value = mcp.tools["cua_tty_observe"](self.SID, True, "NodeCase")
        self.assertEqual(value, {"test_fixture": "delegation-only"})
        self.assertEqual(record, [(("observe", "NodeCase", True), {"session_id": self.SID})])


if __name__ == "__main__":
    unittest.main()
