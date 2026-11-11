"""Seven local/remote MCP tools for the real persistent FrameBridge CUA-TTY."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import socket
import uuid
import zlib

from . import tty
from ._execution_receipt import (
    RECEIPT_FIELDS, UNFINISHED_EXECUTION, execution_error, execution_receipt,
)

TOOL_NAMES = (
    "cua_tty_status", "cua_tty_open", "cua_tty_observe",
    "cua_tty_key_send", "cua_tty_text_type", "cua_tty_resize", "cua_tty_close",
)


def _exec_json(execute, target, arguments):
    script = "python3 -m framebridge_expanded.tty " + " ".join(shlex.quote(str(x)) for x in arguments)
    job_id = "tty-rpc-" + uuid.uuid4().hex[:24]
    digest = hashlib.sha256(script.encode()).hexdigest()
    response = execute(script=script, authorized=True, timeout_s=30, job_id=job_id,
                       sha256=digest, include_stdout=True, target=target)
    if type(response) is not dict:
        return {"ok": False, "error": "TTY_REMOTE_EXEC_FAILED"}
    receipt = execution_receipt(response)
    claimed = receipt.get("execution_state") in UNFINISHED_EXECUTION or any(
        key in receipt for key in ("mcp_job_id", "mcp_script_sha256"))
    if claimed and (receipt.get("mcp_job_id") != job_id or receipt.get("mcp_script_sha256") != digest):
        return {"ok": False, "error": "TTY_REMOTE_RECEIPT_BINDING_INVALID",
                "execution_state": "outcome_unknown", "do_not_replay": True, "mcp_job_id": job_id}
    if response.get("ok") is not True:
        error = "TTY_REMOTE_EXEC_FAILED"
        if receipt.get("execution_state") in UNFINISHED_EXECUTION:
            error = execution_error(response, error)
        return {"ok": False, "error": error, **receipt}
    if response.get("stdout_truncated"):
        return {"ok": False, "error": "TTY_REMOTE_RESPONSE_TRUNCATED", **receipt}
    output = response.get("stdout", "")
    if type(output) is not str:
        return {"ok": False, "error": "TTY_REMOTE_RESPONSE_INVALID", **receipt}
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        value = None
    if type(value) is not dict:
        return {"ok": False, "error": "TTY_REMOTE_RESPONSE_INVALID", **receipt}
    return {**{key: value[key] for key in value if key not in RECEIPT_FIELDS}, **receipt}


def _remote_request(execute, target, payload):
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()).decode("ascii")
    first = _exec_json(execute, target, ["request", "--payload-b64", encoded, "--wire"])
    if first.get("ok") is not True:
        return first
    receipt = execution_receipt(first)
    node_id = receipt.get("node_id")
    read_target = node_id or target
    try:
        ticket = first["ticket"]
        digest = first["sha256"]
        length = first["bytes"]
        count = first["chunks"]
        if (first.get("wire") != "zlib-base64-v1" or type(ticket) is not str
                or re.fullmatch(r"[a-f0-9]{32}", ticket) is None
                or type(digest) is not str or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                or type(length) is not int or not 0 < length <= 16777216
                or type(count) is not int or not 1 <= count <= 20000):
            raise ValueError()
        chunks = []
        for index in range(count):
            part = first if index == 0 else _exec_json(
                execute, read_target, ["read", "--ticket", ticket, "--index", index])
            if index and part.get("ok") is not True:
                delivery = {"error": execution_error(part, "TTY_REMOTE_RESPONSE_INVALID"),
                            **execution_receipt(part)}
                return {"ok": False, "error": "TTY_REMOTE_RESULT_UNAVAILABLE", **receipt,
                        "do_not_replay": True, "result_delivery": delivery}
            if (part.get("ok") is not True or part.get("ticket") != ticket
                    or (node_id is not None and part.get("node_id") != node_id)
                    or part.get("index") != index or part.get("chunks") != count
                    or part.get("sha256") != digest or type(part.get("data")) is not str
                    or len(part["data"]) > 1800):
                raise ValueError()
            chunks.append(part["data"])
        compressed = base64.b64decode("".join(chunks), validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, length + 1)
        if (len(raw) != length or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail or hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError()
        result = json.loads(raw)
        if type(result) is not dict:
            raise ValueError()
        return {**{key: result[key] for key in result if key not in RECEIPT_FIELDS}, **receipt}
    except (KeyError, ValueError, TypeError, zlib.error):
        return {"ok": False, "error": "TTY_REMOTE_RESPONSE_INVALID", **receipt,
                "do_not_replay": True}


def register_tools(mcp, exec_request, *, screen_client=None, image_factory=None):
    """Register tools; exec_request is the existing authorized fleet executor."""
    if screen_client is None:
        from .real_screen.screen_frontdoor import ScreenClient, resolve_profile_route
        if image_factory is None:
            from mcp.server.fastmcp import Image
            image_factory = Image
        screen_client = ScreenClient(exec_request, image_factory, resolve_node=resolve_profile_route)
    def call(payload, target):
        if payload.get("authorized") is not True:
            return {"ok": False, "error": "AUTHORIZED_TRUE_REQUIRED"}
        if type(target) is not str or not target.strip():
            return {"ok": False, "error": "TTY_TARGET_INVALID"}
        requested = target
        target = target.strip()
        if target.upper() == "LOCAL":
            from .fleet import local_target_identity
            host = socket.gethostname()
            try:
                identity = local_target_identity()
            except (OSError, ValueError):
                identity = {"transport": "local", "transport_target": "LOCAL"}
            result = tty.request(os.environ.get("FRAMEBRIDGE_EXPANDED_STATE"), payload)
            result = {**result, **identity, "hostname": host,
                      "executed_hostname": host, "receiver_hostname": host}
        else:
            result = _remote_request(exec_request, target, payload)
        return {**result, "requested_target": requested}

    @mcp.tool()
    def cua_tty_status(authorized: bool = False, target: str = "LOCAL") -> dict:
        """Report the actual CUA-TTY daemon, Bash sessions and configurable resource limits."""
        return call({"op": "status", "authorized": authorized}, target)

    @mcp.tool()
    def cua_tty_open(authorized: bool = False, program: str = "bash",
                     rows: int = 24, cols: int = 80, target: str = "LOCAL") -> dict:
        """Open a real persistent Bash console in the profile's private tmux server; no X11 required."""
        return call({"op": "open", "authorized": authorized, "program": program,
                     "rows": rows, "cols": cols}, target)

    @mcp.tool()
    def cua_tty_observe(session_id: str, authorized: bool = False,
                        target: str = "LOCAL"):
        """Capture the entire actual active screen and report whether this session is visible."""
        return screen_client.call("observe", target, authorized, session_id=session_id)

    @mcp.tool()
    def cua_tty_key_send(session_id: str, key: str, authorized: bool = False,
                         target: str = "LOCAL", capture_id: str = ""):
        """Send a key only to the visible TTY target bound to a real capture; use graphical CUA for X11."""
        return screen_client.call("key", target, authorized, session_id=session_id,
                                  capture_id=capture_id, key=key)

    @mcp.tool()
    def cua_tty_text_type(session_id: str, text: str, authorized: bool = False,
                          target: str = "LOCAL", capture_id: str = ""):
        """Type only into the visible TTY target bound to a real capture; never into a hidden session."""
        return screen_client.call("text", target, authorized, session_id=session_id,
                                  capture_id=capture_id, text=text)

    @mcp.tool()
    def cua_tty_resize(session_id: str, rows: int, cols: int,
                       authorized: bool = False, target: str = "LOCAL") -> dict:
        """Resize the real PTY/tmux console within the limits reported by cua_tty_status."""
        return call({"op": "resize", "authorized": authorized, "session_id": session_id,
                     "rows": rows, "cols": cols}, target)

    @mcp.tool()
    def cua_tty_close(session_id: str, authorized: bool = False,
                      target: str = "LOCAL") -> dict:
        """Close only the selected console owned by this runtime and verify tmux cleanup."""
        return call({"op": "close", "authorized": authorized, "session_id": session_id}, target)

    return TOOL_NAMES
