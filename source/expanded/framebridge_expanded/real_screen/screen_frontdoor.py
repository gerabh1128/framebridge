"""Original FrameBridge real-screen MCP adapter.

The node captures pixels. This adapter validates and transports those bytes;
it never renders terminal text into an image.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import shlex
import struct
import time
import uuid
from typing import Callable

from .._execution_receipt import (
    NOT_STARTED_EXECUTION, RECEIPT_FIELDS, UNFINISHED_EXECUTION, execution_error, execution_receipt,
)
from ..fleet import FleetError, select_target
from .screen_capture import MAX_BULK_CHUNK_BYTES

CHUNK_BYTES = 2100
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_STDOUT_BYTES = 4096
# Only the explicit typed screen transport preserves this larger stdout budget.
EXPANDED_STDOUT_BYTES = 65536
EXPANDED_CHUNK_BYTES = 45000
# A 700,000-byte chunk leaves room for base64, result and signature metadata
# within the broker's unchanged 1 MiB signed POST body limit.
BULK_STDOUT_BYTES = 1_000_000
TRANSFER_TIMEOUT_SECONDS = 45
RPC_COMMAND = '/usr/bin/python3 -B -m framebridge_expanded.real_screen.screen_rpc '
HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
SID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
ERROR = re.compile(r"[A-Z0-9_]{1,100}\Z")
READ = {"readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": False}
CHANGE = {**READ, "readOnlyHint": False}
# These explicit backend checks run before an input capture is consumed. Generic
# dispatch failures do not prove whether input was sent, even with valid framing.
INPUT_REJECTION_CODES = {
    "REQUEST_INVALID", "OPERATION_INVALID", "UNKNOWN_REQUEST_FIELDS",
    "SESSION_ID_INVALID", "KEY_INVALID", "TEXT_INVALID", "CAPTURE_ID_INVALID",
    "CAPTURE_RECORD_INVALID", "CAPTURE_RECORD_MISMATCH",
    "USE_GRAPHICAL_CUA_FOR_X11_INPUT", "VISIBLE_INPUT_UNAVAILABLE",
    "CAPTURE_ALREADY_USED_OBSERVE_AGAIN", "SESSION_NOT_VISIBLE",
}


class ScreenError(RuntimeError):
    """A bounded public error suitable for the MCP response."""

    def __init__(self, code, details=None, *, backend_rejection=False):
        super().__init__(code)
        self.details = details or {}
        # Local proof, never accepted from a remote JSON field.
        self.backend_rejection = backend_rejection


def require(ok, code):
    if not ok:
        raise ScreenError(code)


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def valid_reference(value, regex, optional=False):
    return type(value) is str and ((optional and value == "") or regex.fullmatch(value) is not None)


def _response_envelope(outer, stdout_limit_bytes):
    stdout = outer.get("stdout")
    require(type(stdout) is str and outer.get("stdout_truncated") is not True,
            "SCREEN_STDOUT_INVALID")
    try:
        require(len(stdout.encode("utf-8")) <= stdout_limit_bytes, "SCREEN_STDOUT_INVALID")
        envelope = json.loads(stdout)
    except (ValueError, UnicodeError, RecursionError):
        raise ScreenError("SCREEN_RESPONSE_INVALID") from None
    require(type(envelope) is dict and type(envelope.get("ok")) is bool,
            "SCREEN_RESPONSE_INVALID")
    return envelope


class _AuthenticatedReply(dict):
    """A payload tied to the EXEC recipient verified by this client instance.

    The hostname in the payload is observed screen identity. The route
    alias and UUID come from the existing authenticated executor envelope.
    A JSON object cannot supply this local proof or change its capture identity.
    """

    _IDENTITY_FIELDS = ("capture_id", "node", "sha256", "session_id", "bytes",
                        "width", "height", "source", "screen_type", "platform",
                        "full_screen", "authentic_pixels", "max_chunk_bytes",
                        "max_chunk_bytes_v2", "pixel_source_verified", "synthetic")

    def __init__(self, payload, owner, node, node_id, receipt):
        super().__init__(payload)
        self.__owner, self.__node, self.__node_id = owner, node, node_id
        self.__identity = tuple(self.get(key) for key in self._IDENTITY_FIELDS)
        self.__receipt = receipt

    def bound_to(self, owner, node, node_id=None):
        return (self.__owner is owner and self.__node == node
                and (node_id is None or self.__node_id == node_id)
                and self.__identity == tuple(self.get(key) for key in self._IDENTITY_FIELDS))

    @property
    def node_id(self):
        return self.__node_id

    @property
    def receipt(self):
        return dict(self.__receipt)


class ScreenClient:
    """Transport only; execution policy and node resolution stay in the host.

    The monotonic transfer deadline bounds admission of RPCs and successful
    publication. It is cooperative: the executor can spend its existing bounded
    transport grace on the final in-flight RPC. Late replies never become a
    successful image, and no further chunks are requested after the deadline.
    """

    def __init__(self, exec_request: Callable, image_factory: Callable,
                 resolve_node: Callable | None = None, *,
                 stdout_limit_bytes: int = MAX_STDOUT_BYTES,
                 transfer_timeout_s: int = TRANSFER_TIMEOUT_SECONDS):
        if (type(stdout_limit_bytes) is not int or
                stdout_limit_bytes not in (MAX_STDOUT_BYTES, EXPANDED_STDOUT_BYTES, BULK_STDOUT_BYTES)):
            raise ValueError("unsupported screen stdout budget")
        if type(transfer_timeout_s) is not int or not 1 <= transfer_timeout_s <= 120:
            raise ValueError("screen transfer timeout must be 1..120 seconds")
        self.stdout_limit_bytes = stdout_limit_bytes
        self.chunk_bytes = (EXPANDED_CHUNK_BYTES
                            if stdout_limit_bytes >= EXPANDED_STDOUT_BYTES else CHUNK_BYTES)
        self.transfer_timeout_s = transfer_timeout_s
        self.exec_request = exec_request
        self.image_factory = image_factory
        self.resolve_node = resolve_node or (lambda node: node.upper())
        self._reply_owner = object()

    def remote(self, node, request, *, deadline=None, submitted=None, expected_node_id=None):
        timeout_s = 30
        if deadline is not None:
            remaining = deadline - time.monotonic()
            # The authenticated EXEC protocol accepts whole seconds only. Rounding
            # down prevents any individual request exceeding the remaining budget.
            require(remaining >= 1, "SCREEN_TRANSFER_TIMEOUT")
            timeout_s = min(timeout_s, int(remaining))
        payload = base64.b64encode(encode(request)).decode("ascii")
        script = RPC_COMMAND + shlex.quote(payload) + "\n"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        job_id = "cua-screen-" + uuid.uuid4().hex
        if submitted is not None:
            submitted["attempted"] = True
        outer = self.exec_request(script=script, authorized=True, timeout_s=timeout_s,
                                  job_id=job_id, sha256=digest,
                                  include_stdout=True, target=node)
        require(type(outer) is dict, "SCREEN_EXEC_FAILED")
        receipt = execution_receipt(outer)
        state = receipt.get("execution_state")
        bound = (outer.get("requested_target", outer.get("target")) == node and
                 outer.get("mcp_job_id") == job_id and outer.get("mcp_script_sha256") == digest)
        if outer.get("ok") is False and state in UNFINISHED_EXECUTION:
            require(bound and (expected_node_id is None or outer.get("node_id") == expected_node_id),
                    "SCREEN_EXEC_BINDING_INVALID")
            rejected = state in NOT_STARTED_EXECUTION
            if rejected:
                receipt["performed"] = False
            error = execution_error(outer, "SCREEN_EXEC_FAILED")
            if (error == "SCREEN_EXEC_FAILED" and state == "outcome_unknown"
                    and outer.get("request_type") == "screen_rpc"
                    and type(outer.get("exit_code")) in (int, str)
                    and str(outer["exit_code"]) == "1"):
                # Native Windows returns a typed error and a replay barrier
                # together. Keep the backend error without weakening the barrier.
                try:
                    response = _response_envelope(outer, min(self.stdout_limit_bytes, EXPANDED_STDOUT_BYTES))
                except ScreenError:
                    pass
                else:
                    details = response.get("error")
                    if (response["ok"] is False and type(details) is dict
                            and valid_reference(details.get("code"), ERROR)):
                        error = details["code"]
            raise ScreenError(error, receipt, backend_rejection=rejected)
        if deadline is not None:
            require(time.monotonic() < deadline, "SCREEN_TRANSFER_TIMEOUT")
        require(type(outer.get("exit_code")) in (int, str) and
                str(outer["exit_code"]) in ("0", "1"), "SCREEN_EXEC_FAILED")
        require(bound, "SCREEN_EXEC_BINDING_INVALID")
        try:
            node_id = outer.get("node_id")
            require(type(node_id) is str, "SCREEN_EXEC_NODE_ID_INVALID")
            node_id = str(uuid.UUID(node_id))
        except (ValueError, AttributeError):
            raise ScreenError("SCREEN_EXEC_NODE_ID_INVALID") from None
        require(expected_node_id is None or node_id == expected_node_id, "SCREEN_EXEC_BINDING_INVALID")
        limit = self.stdout_limit_bytes
        if request["op"] != "read_chunk" or "bulk_chunk_bytes" not in request:
            limit = min(limit, EXPANDED_STDOUT_BYTES)
        envelope = _response_envelope(outer, limit)
        if not envelope["ok"]:
            code = envelope.get("error")
            details = {}
            if type(code) is dict:
                for name in ("performed", "capture_consumed", "observe_again"):
                    if type(code.get(name)) is bool:
                        details[name] = code[name]
                if valid_reference(code.get("capture_id"), HEX_ID):
                    details["capture_id"] = code["capture_id"]
                if valid_reference(code.get("cause"), ERROR):
                    details["cause"] = code["cause"]
                code = code.get("code")
            valid_code = valid_reference(code, ERROR)
            raise ScreenError(code if valid_code else "SCREEN_BACKEND_FAILED", details,
                              backend_rejection=(valid_code and outer.get("ok") is False
                                                 and str(outer["exit_code"]) == "1"
                                                 and (code in INPUT_REJECTION_CODES
                                                      or details.get("performed") is False)))
        require(outer.get("ok") is True and str(outer["exit_code"]) == "0", "SCREEN_EXEC_FAILED")
        require(type(envelope.get("result")) is dict, "SCREEN_RESPONSE_INVALID")
        return _AuthenticatedReply(envelope["result"], self._reply_owner, node, node_id, receipt)

    def image(self, node, manifest, *, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + self.transfer_timeout_s
        capture_id = manifest.get("capture_id")
        total, digest = manifest.get("bytes"), manifest.get("sha256")
        require(valid_reference(capture_id, HEX_ID) and valid_reference(digest, SHA),
                "SCREEN_MANIFEST_INVALID")
        require(type(total) is int and 0 < total <= MAX_IMAGE_BYTES, "SCREEN_IMAGE_SIZE_INVALID")
        require(isinstance(manifest, _AuthenticatedReply) and
                manifest.bound_to(self._reply_owner, node), "SCREEN_IMAGE_NODE_MISMATCH")
        require(valid_reference(manifest.get("node"), NODE), "SCREEN_IMAGE_HOSTNAME_INVALID")
        width, height = manifest.get("width"), manifest.get("height")
        require(type(width) is int and type(height) is int and
                0 < width <= 16384 and 0 < height <= 16384,
                "SCREEN_DIMENSIONS_INVALID")
        source = manifest.get("source")
        require(source in ("linux-framebuffer", "x11-root-XGetImage", "windows-gdi-BitBlt"),
                "SCREEN_PIXEL_SOURCE_INVALID")
        require(manifest.get("full_screen") is True and manifest.get("authentic_pixels") is True,
                "SCREEN_PIXEL_PROVENANCE_INVALID")
        if source == "windows-gdi-BitBlt":
            session, display = manifest.get("session"), manifest.get("display_context")
            require(manifest.get("platform") == "windows" and manifest.get("screen_type") == "windows"
                    and manifest.get("pixel_source_verified") is True and manifest.get("synthetic") is False
                    and type(session) is dict and session.get("session_type") == "windows"
                    and session.get("physical_console") is True
                    and type(display) is dict and display.get("kind") == "physical-windows"
                    and display.get("synthetic") is False, "SCREEN_PIXEL_PROVENANCE_INVALID")
        # Capability belongs to this authenticated manifest, never a shared cache.
        # A new central must retain the old request schema for a frozen receiver.
        chunk_bytes = CHUNK_BYTES
        if "max_chunk_bytes" in manifest:
            capability = manifest["max_chunk_bytes"]
            require(type(capability) is int and 1 <= capability <= EXPANDED_CHUNK_BYTES,
                    "SCREEN_TRANSFER_CAPABILITY_INVALID")
            chunk_bytes = min(self.chunk_bytes, capability)
        if self.stdout_limit_bytes == BULK_STDOUT_BYTES and "max_chunk_bytes_v2" in manifest:
            bulk_bytes = manifest["max_chunk_bytes_v2"]
            require(type(bulk_bytes) is int and EXPANDED_CHUNK_BYTES < bulk_bytes <= MAX_BULK_CHUNK_BYTES,
                    "SCREEN_TRANSFER_CAPABILITY_INVALID")
            chunk_bytes = bulk_bytes
        parts = []
        count = math.ceil(total / chunk_bytes)
        for index in range(count):
            request = {"op": "read_chunk", "capture_id": capture_id,
                       "index": index, "expected_sha256": digest}
            # Omit the extension entirely on constrained legacy transports.
            if chunk_bytes > EXPANDED_CHUNK_BYTES:
                request["bulk_chunk_bytes"] = chunk_bytes
            elif chunk_bytes != CHUNK_BYTES:
                request["chunk_bytes"] = chunk_bytes
            part = self.remote(node, request, deadline=deadline)
            require(isinstance(part, _AuthenticatedReply) and
                    part.bound_to(self._reply_owner, node, manifest.node_id),
                    "SCREEN_CHUNK_NODE_MISMATCH")
            require(part.get("capture_id") == capture_id and type(part.get("index")) is int and
                    part["index"] == index and type(part.get("total_chunks")) is int and
                    part["total_chunks"] == count, "SCREEN_CHUNK_BINDING_INVALID")
            require(type(part.get("bytes")) is int and part["bytes"] == total and
                    part.get("sha256") == digest and part.get("encoding") == "base64" and
                    type(part.get("more")) is bool and part["more"] == (index + 1 < count),
                    "SCREEN_CHUNK_MANIFEST_INVALID")
            text = part.get("data")
            require(type(text) is str and len(text) <= 4 * math.ceil(chunk_bytes / 3), "SCREEN_CHUNK_INVALID")
            try:
                data = base64.b64decode(text, validate=True)
            except (ValueError, UnicodeError, binascii.Error):
                raise ScreenError("SCREEN_CHUNK_INVALID") from None
            require(len(data) == min(chunk_bytes, total - index * chunk_bytes),
                    "SCREEN_CHUNK_LENGTH_INVALID")
            parts.append(data)
        data = b"".join(parts)
        require(len(data) == total and hashlib.sha256(data).hexdigest() == digest,
                "SCREEN_IMAGE_HASH_MISMATCH")
        require(len(data) >= 33 and data[:8] == b"\x89PNG\r\n\x1a\n" and
                data[12:16] == b"IHDR" and struct.unpack(">II", data[16:24]) == (width, height),
                "SCREEN_PNG_INVALID")
        require(time.monotonic() < deadline, "SCREEN_TRANSFER_TIMEOUT")
        return self.image_factory(data=data, format="png")

    def call(self, op, node, authorized, *, target=None, **kwargs):
        requested_node = target if target is not None else (node if node is not None else "")
        action_confirmed = False
        input_reply_received = False
        manifest = None
        submitted = {}
        deadline = time.monotonic() + self.transfer_timeout_s
        try:
            require(authorized is True, "AUTHORIZED_TRUE_REQUIRED")
            try:
                node = select_target(target, node)
            except FleetError as exc:
                raise ScreenError(str(exc)) from None
            requested_node = node
            require(valid_reference(node, NODE), "SCREEN_NODE_INVALID")
            route = self.resolve_node(node)
            if isinstance(route, tuple):
                node, node_id = route
                require(valid_reference(node_id, NODE), "SCREEN_EXEC_NODE_ID_INVALID")
            else:
                node, node_id = route, None
            require(valid_reference(node, NODE), "SCREEN_NODE_INVALID")
            dispatch_target = node_id or node
            if kwargs.get("session_id"):
                require(valid_reference(kwargs["session_id"], SID), "SCREEN_SESSION_INVALID")
            else:
                kwargs.pop("session_id", None)
            if op in ("key", "text", "read_capture"):
                require(valid_reference(kwargs.get("capture_id"), HEX_ID), "SCREEN_CAPTURE_ID_REQUIRED")
            if op == "key":
                require(type(kwargs.get("key")) is str and 0 < len(kwargs["key"]) <= 32,
                        "SCREEN_KEY_INVALID")
            if op == "text":
                require(type(kwargs.get("text")) is str and 0 < len(kwargs["text"].encode("utf-8")) <= 4096,
                        "SCREEN_TEXT_INVALID")
            manifest = self.remote(dispatch_target, {"op": op, **kwargs}, deadline=deadline,
                                   submitted=submitted, expected_node_id=node_id)
            input_reply_received = op in ("key", "text")
            action = manifest.get("action")
            summary = action.get("result") if type(action) is dict else None
            action_confirmed = (op in ("key", "text") and type(action) is dict and
                                action.get("kind") == op and
                                action.get("previous_capture_id") == kwargs["capture_id"] and
                                type(summary) is dict and summary.get("performed") is True)
            if op == "read_capture":
                require(manifest.get("capture_id") == kwargs["capture_id"],
                        "SCREEN_CAPTURE_REFERENCE_MISMATCH")
            image = self.image(dispatch_target, manifest, deadline=deadline)
            require(time.monotonic() < deadline, "SCREEN_TRANSFER_TIMEOUT")
            node = manifest.receipt.get("resolved_target", node)
            metadata = {**manifest.receipt,
                        "ok": True, "target": node, "requested_target": requested_node,
                        "node": node, "requested_node": requested_node,
                        "resolved_target": node,
                        "node_id": manifest.node_id, "hostname": manifest["node"],
                        "observed_hostname": manifest["node"],
                        "result": dict(manifest, transport_node=node, node_id=manifest.node_id)}
            metadata["result"].pop("path", None)
            metadata["result"].pop("image_base64", None)
            metadata["result"]["image_mime"] = "image/png"
            metadata["result"]["full_screen"] = True
            metadata["result"]["authentic_pixels"] = True
            return [metadata, image]
        except ScreenError as exc:
            details = dict(exc.details)
            if manifest is not None:
                delivery_receipt = execution_receipt(details)
                details = {key: value for key, value in details.items()
                           if key not in RECEIPT_FIELDS and key != "performed"}
                if delivery_receipt:
                    details["result_delivery"] = {"error": str(exc), **delivery_receipt}
                details.update(manifest.receipt)
                if op in ("observe", "read_capture") and str(exc) == "SCREEN_TRANSFER_TIMEOUT":
                    details.update(capture_id=manifest["capture_id"], capture_available=True)
            if op in ("key", "text") and (input_reply_received or
                    (submitted.get("attempted") and not exc.backend_rejection)):
                details.setdefault("observe_again", True)
                details["do_not_replay"] = True
                if action_confirmed:
                    details.update(performed=True, capture_consumed=True)
                details.setdefault("capture_id", kwargs["capture_id"])
            return {"ok": False, "node": requested_node, "error": str(exc),
                    **details, "requested_target": requested_node}
        except Exception:
            details = {}
            if op in ("key", "text") and submitted.get("attempted"):
                details.update(observe_again=True, do_not_replay=True, capture_id=kwargs["capture_id"])
                if action_confirmed:
                    details.update(performed=True, capture_consumed=True)
            return {"ok": False, "node": requested_node,
                    "error": "SCREEN_BACKEND_UNAVAILABLE", **details}


def _resolve_profile_descriptor(alias):
    from ..fleet import resolve_node
    try:
        return resolve_node(alias)
    except FleetError as exc:
        raise ScreenError(str(exc)) from None


def resolve_profile_node(alias):
    """Canonicalize only through the enrolled profile registry, never guessed hostnames."""
    return _resolve_profile_descriptor(alias)["alias"]


def resolve_profile_route(selector):
    """Keep the public alias and dispatch screen operations to the enrolled UUID."""
    descriptor = _resolve_profile_descriptor(selector)
    return descriptor["alias"], descriptor["node_id"]


def register_framebridge_screen_tools(mcp, exec_request, *, image_factory=None,
                                     resolve_node=None,
                                     stdout_limit_bytes=MAX_STDOUT_BYTES,
                                     transfer_timeout_s=TRANSFER_TIMEOUT_SECONDS):
    """Register real image tools. The host supplies its existing executor."""
    if image_factory is None:
        from mcp.server.fastmcp import Image
        image_factory = Image
    client = ScreenClient(exec_request, image_factory, resolve_node,
                          stdout_limit_bytes=stdout_limit_bytes,
                          transfer_timeout_s=transfer_timeout_s)

    @mcp.tool(annotations=dict(READ))
    def framebridge_screen_observe(node: str = None, authorized: bool = False,
                                   session_id: str = "", *, target: str = None):
        """Capture target's entire active screen as real pixels.

        Requires the target's typed screen RPC route. For a Windows node with
        graphical CUA enabled, CUA OBSERVE with capture_scope=ROOT is a separate operation.
        To display or redisplay this original in ChatGPT, call framebridge_screen_show
        with the returned target and capture_id. Do not take another capture to show it.
        """
        return client.call("observe", node, authorized, target=target, session_id=session_id)

    @mcp.tool(annotations=dict(CHANGE))
    def framebridge_screen_key(node: str = None, capture_id: str = "", key: str = "",
                               authorized: bool = False, session_id: str = "", *, target: str = None):
        """Send a key to target's visible TTY bound to a real capture. Use graphical CUA for X11."""
        return client.call("key", node, authorized, target=target, capture_id=capture_id,
                           key=key, session_id=session_id)

    @mcp.tool(annotations=dict(CHANGE))
    def framebridge_screen_text(node: str = None, capture_id: str = "", text: str = "",
                                authorized: bool = False, session_id: str = "", *, target: str = None):
        """Type into target's visible TTY bound to a real capture. Use graphical CUA for X11."""
        return client.call("text", node, authorized, target=target, capture_id=capture_id,
                           text=text, session_id=session_id)

    return client
